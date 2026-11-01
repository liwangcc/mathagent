"""Durable, low-priority knowledge classification.

The worker deliberately uses one Qwen Flash text request at a time. Imports are never held
open while the provider is called; question-bank rows and archived wrong
questions are stored first and classified later. A short database lease keeps
both queues on one serial lane across web workers and process restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.orm import aliased

from config import (
    KNOWLEDGE_CLASSIFIER_BATCH_SIZE,
    KNOWLEDGE_CLASSIFIER_LEASE_S,
    KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S,
    KNOWLEDGE_CLASSIFIER_WORKER_ENABLED,
)
from database import SessionLocal
from models import (
    GradingResult,
    KnowledgeEvent,
    QuestionBank,
    QuestionClassificationJob,
    StudentProfile,
)
from models_correction import KnowledgeEventClassificationJob, QuestionAttemptEvent
from services.teaching_context import make_teaching_context, validate_teaching_context, use_teaching_context
from services.math_curriculum import get_knowledge_point
from services.correction_tracking import attempt_teaching_context, stamp_teaching_snapshot, grading_result_teaching_context

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_RETRY_WAITING = "retry_waiting"
STATUS_COMPLETED = "completed"
STATUS_MANUAL = "manual"
UNCLASSIFIED_KNOWLEDGE_VALUES = ("", "未知", "未指定", "未标注", "未分类", "综合")
REJECTED_ERROR = "模型多次未能从知识点目录中选出结果，请手动选择知识点"


def _record_context(record) -> dict:
    stage = str(getattr(record, "education_stage", "") or "")
    version = str(getattr(record, "catalog_version", "") or "")
    return _claimed_context({"education_stage": stage, "catalog_version": version})


def _claimed_context(item: dict) -> dict:
    if "teaching_context" in item:
        return validate_teaching_context(item["teaching_context"])
    stage, version = str(item.get("education_stage") or ""), str(item.get("catalog_version") or "")
    if not stage:
        if version:
            raise ValueError("旧记录包含无法归属的知识点目录版本")
        return {}
    context = make_teaching_context(stage)
    if version != context["curriculum_version"]:
        raise ValueError("知识点目录版本不可用，请人工迁移后重新分类")
    return context


def _attempt_context(attempt: QuestionAttemptEvent, db=None) -> dict:
    return attempt_teaching_context(db, attempt) if db is not None else _claimed_context(attempt.question_snapshot())


def _valid_pair(chapter, topic, context: dict):
    pair = _knowledge_pair(chapter, topic)
    if pair and context and get_knowledge_point(context["education_stage"], *pair) is None:
        return None
    return pair


async def _classify_stage_groups(claimed: list[dict], id_key: str) -> list[dict]:
    """One text-only batch per frozen catalogue, never mix stage prompts.

    Validation is per question, the same mode the grading workflow uses: an
    off-catalogue answer becomes one ``rejected`` row that is retried alone
    instead of discarding every valid label in the batch.
    """
    from services import analyzer
    groups = {}
    results = []
    for item in claimed:
        try:
            context = _claimed_context(item)
        except ValueError as exc:
            results.append({"id": str(item[id_key]), "status": STATUS_MANUAL, "error": str(exc)})
            continue
        key = json.dumps(context, sort_keys=True)
        groups.setdefault(key, (context, []))[1].append(item)
    for context, items in groups.values():
        with use_teaching_context(context):
            results.extend(await analyzer.classify_questions_knowledge([
                {"id": str(item[id_key]), "question": item["question"]} for item in items
            ], per_question=True))
    return results

# At most five delayed retries.  The final delay is one hour; after that the
# question is visible in the manual queue instead of retrying forever.
RETRY_DELAYS_SECONDS = (60, 300, 900, 1800, 3600)
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)


def classifier_worker_configured() -> bool:
    """Return whether the worker is enabled and the shared Flash route is ready."""
    if not KNOWLEDGE_CLASSIFIER_WORKER_ENABLED:
        return False
    from services.flash_grading import get_flash_readiness

    return bool(get_flash_readiness().get("ready"))


def _now() -> datetime:
    # The existing SQLite schema uses naive local datetimes throughout.
    return datetime.now()


def _worker_id() -> str:
    host = socket.gethostname().replace(" ", "_")[:24] or "host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _retry_delay(attempts: int) -> int:
    """Return the next delay after a failed attempt (attempts starts at one)."""
    index = max(0, min(int(attempts) - 1, len(RETRY_DELAYS_SECONDS) - 1))
    return int(RETRY_DELAYS_SECONDS[index])


def _question_is_unclassified(question: QuestionBank) -> bool:
    return (
        str(question.chapter or "").strip() in UNCLASSIFIED_KNOWLEDGE_VALUES
        or str(question.topic or "").strip() in UNCLASSIFIED_KNOWLEDGE_VALUES
    )


def _column_is_unclassified(column):
    return func.trim(func.coalesce(column, "")).in_(UNCLASSIFIED_KNOWLEDGE_VALUES)


def _knowledge_pair(chapter: object, topic: object) -> tuple[str, str] | None:
    chapter_text = str(chapter or "").strip()
    topic_text = str(topic or "").strip()
    if (
        chapter_text in UNCLASSIFIED_KNOWLEDGE_VALUES
        or topic_text in UNCLASSIFIED_KNOWLEDGE_VALUES
    ):
        return None
    return chapter_text, topic_text


def _set_mapping_knowledge(mapping: dict, chapter: str, topic: str, context=None) -> bool:
    context = _claimed_context(mapping) if context is None else context
    if _knowledge_pair(
        mapping.get("chapter"),
        mapping.get("topic") or mapping.get("knowledge_point"),
    ) is not None:
        before = dict(mapping)
        stamp_teaching_snapshot(mapping, context)
        return before != mapping
    mapping["chapter"] = chapter
    mapping["topic"] = topic
    mapping["knowledge_point"] = topic
    stamp_teaching_snapshot(mapping, context)
    return True


def _attempt_question_text(attempt: QuestionAttemptEvent) -> str:
    snapshot = attempt.question_snapshot()
    return str(snapshot.get("question_text") or snapshot.get("question") or "").strip()


def _attempt_parent_number(detail: dict, index: int) -> str:
    return str(
        detail.get("parent_question_no")
        or detail.get("question_no")
        or detail.get("question_number")
        or index + 1
    ).strip()


def _propagate_grading_result_knowledge(
    db,
    *,
    grading_result_id: int,
    attempts: list[QuestionAttemptEvent],
    chapter: str,
    topic: str,
) -> None:
    """Update only projections that can be tied to the classified original."""
    grading_result = db.get(GradingResult, int(grading_result_id))
    if grading_result is None:
        return
    context = grading_result_teaching_context(grading_result)
    if _valid_pair(chapter, topic, context) is None:
        return
    details = grading_result.get_details()
    if not isinstance(details, list):
        details = []
    target_numbers = {str(row.question_no or "").strip() for row in attempts}
    target_indexes: list[int] = []
    details_changed = False
    for index, detail in enumerate(details):
        if not isinstance(detail, dict):
            continue
        if _attempt_parent_number(detail, index) not in target_numbers:
            continue
        target_indexes.append(index)
        # Classification may improve derived labels, but must not introduce a
        # stage into an adopted legacy contract/fingerprint input.
        frozen_context = _claimed_context(detail) if detail.get("teaching_context") else {}
        details_changed = _set_mapping_knowledge(detail, chapter, topic, frozen_context) or details_changed
    if details_changed:
        grading_result.set_details(details)
    if target_indexes:
        for profile in db.query(StudentProfile).filter(
            StudentProfile.grading_result_id == grading_result.id,
        ).all():
            profile.education_stage = context.get("education_stage", "")
            profile.catalog_version = context.get("curriculum_version", "")
            profile.set_details(details)

    events = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.grading_result_id == grading_result.id,
    ).order_by(KnowledgeEvent.id.asc()).all()
    event_ids: set[int] = set()
    if details and len(events) == len(details):
        event_ids.update(events[index].id for index in target_indexes)

    question_bank_ids = {
        int(row.question_bank_id)
        for row in attempts
        if row.question_bank_id is not None
    }
    if question_bank_ids:
        event_ids.update(
            row.id for row in events
            if row.question_bank_id in question_bank_ids
        )

    # One archived top-level question may legitimately produce several
    # subquestion events. If it is the only attempt, every event belongs to it.
    attempt_count = db.query(func.count(QuestionAttemptEvent.id)).filter(
        QuestionAttemptEvent.grading_result_id == grading_result.id,
    ).scalar()
    if int(attempt_count or 0) == 1:
        event_ids.update(row.id for row in events)

    for event in events:
        if event.id not in event_ids:
            continue
        if _knowledge_pair(event.chapter, event.topic) is None:
            event.chapter = chapter
            event.topic = topic
            event.education_stage = context.get("education_stage", "")
            event.catalog_version = context.get("curriculum_version", "")
            point = get_knowledge_point(context["education_stage"], chapter, topic) if context else None
            event.knowledge_point_id = point["id"] if point else ""


def _propagate_fingerprint_knowledge(
    db,
    *,
    question_fingerprint: str,
    chapter: str,
    topic: str,
    classification_source: str = "model",
    classification_confidence: str = "medium",
    classification_version: str = "",
) -> int:
    """Persist one classification across every projection of the same question."""
    pair = _knowledge_pair(chapter, topic)
    if pair is None:
        return 0
    chapter, topic = pair
    attempts = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.question_fingerprint == question_fingerprint,
    ).order_by(QuestionAttemptEvent.id.asc()).all()
    if not attempts:
        return 0
    try:
        contexts = [_attempt_context(attempt, db) for attempt in attempts]
        context = contexts[0]
        if any(value != context for value in contexts) or _valid_pair(chapter, topic, context) is None:
            return 0
    except ValueError:
        return 0

    changed = 0
    by_result: dict[int, list[QuestionAttemptEvent]] = {}
    question_bank_ids: set[int] = set()
    for attempt in attempts:
        snapshot = attempt.question_snapshot()
        if _set_mapping_knowledge(snapshot, chapter, topic):
            attempt.question_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
            changed += 1
        by_result.setdefault(int(attempt.grading_result_id), []).append(attempt)
        if attempt.question_bank_id is not None:
            question_bank_ids.add(int(attempt.question_bank_id))

    for grading_result_id, result_attempts in by_result.items():
        _propagate_grading_result_knowledge(
            db,
            grading_result_id=grading_result_id,
            attempts=result_attempts,
            chapter=chapter,
            topic=topic,
        )

    now = _now()
    if question_bank_ids:
        questions = db.query(QuestionBank).filter(
            QuestionBank.id.in_(question_bank_ids),
        ).all()
        compatible_bank_ids = set()
        for question in questions:
            try:
                if _record_context(question) != context:
                    continue
            except ValueError:
                continue
            compatible_bank_ids.add(int(question.id))
            if not _question_is_unclassified(question):
                continue
            question.chapter = chapter
            question.topic = topic
            point = get_knowledge_point(context["education_stage"], chapter, topic) if context else None
            question.knowledge_point_id = point["id"] if point else ""
            question.classification_status = (
                "suggested" if classification_source == "model" else "confirmed"
            )
            question.classification_source = classification_source[:20]
            question.classification_confidence = classification_confidence[:10]
            question.classification_version = classification_version[:64]
            question.classification_error = ""
            question.classified_at = now
        db.query(QuestionClassificationJob).filter(
            QuestionClassificationJob.question_id.in_(compatible_bank_ids),
            QuestionClassificationJob.status.in_((
                STATUS_PENDING,
                STATUS_RETRY_WAITING,
                STATUS_RUNNING,
                STATUS_MANUAL,
            )),
        ).update(
            {
                QuestionClassificationJob.status: STATUS_COMPLETED,
                QuestionClassificationJob.locked_until: None,
                QuestionClassificationJob.worker_id: "",
                QuestionClassificationJob.completed_at: now,
                QuestionClassificationJob.updated_at: now,
            },
            synchronize_session=False,
        )

    db.query(KnowledgeEventClassificationJob).filter(
        KnowledgeEventClassificationJob.question_fingerprint == question_fingerprint,
        KnowledgeEventClassificationJob.status.in_((
            STATUS_PENDING,
            STATUS_RETRY_WAITING,
            STATUS_RUNNING,
            STATUS_MANUAL,
        )),
    ).update(
        {
            KnowledgeEventClassificationJob.status: STATUS_COMPLETED,
            KnowledgeEventClassificationJob.locked_until: None,
            KnowledgeEventClassificationJob.worker_id: "",
            KnowledgeEventClassificationJob.completed_at: now,
            KnowledgeEventClassificationJob.updated_at: now,
        },
        synchronize_session=False,
    )
    return changed


def _trusted_attempt_group_knowledge(
    db,
    attempts: list[QuestionAttemptEvent],
) -> tuple[str, str] | None:
    """Return an existing unambiguous classification without calling the classifier."""
    pairs: set[tuple[str, str]] = set()
    try:
        contexts = [_attempt_context(attempt, db) for attempt in attempts]
        context = contexts[0] if contexts else {}
        if any(value != context for value in contexts):
            return None
    except ValueError:
        return None
    for attempt in attempts:
        snapshot = attempt.question_snapshot()
        pair = _valid_pair(
            snapshot.get("chapter"),
            snapshot.get("topic") or snapshot.get("knowledge_point"),
            context,
        )
        if pair is not None:
            pairs.add(pair)
    question_bank_ids = {
        int(row.question_bank_id)
        for row in attempts
        if row.question_bank_id is not None
    }
    if question_bank_ids:
        for question in db.query(QuestionBank).filter(
            QuestionBank.id.in_(question_bank_ids),
        ).all():
            try:
                if _record_context(question) != context:
                    continue
            except ValueError:
                continue
            pair = _valid_pair(question.chapter, question.topic, context)
            if pair is not None:
                pairs.add(pair)
    return next(iter(pairs)) if len(pairs) == 1 else None


def grading_knowledge_for_detail(
    db,
    *,
    grading_result_id: int,
    detail_index: int,
    detail: dict,
    context: dict,
) -> dict:
    """Reuse the grading workflow's Flash label for one archived question.

    Never calls a model. ``status`` is ``classified`` with the single
    catalogue-valid label, ``pending`` while the archive job for this question
    can still produce one, or ``failed`` when grading left it unlabelled or
    with conflicting labels; the teacher then chooses one by hand.
    """
    from services.correction_tracking import question_knowledge

    attempts = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == int(grading_result_id),
        QuestionAttemptEvent.question_no == _attempt_parent_number(detail, detail_index),
    ).all()
    pairs: set[tuple[str, str]] = set()
    for attempt in attempts:
        knowledge = question_knowledge(db, attempt)
        pair = _valid_pair(knowledge["chapter"], knowledge["topic"], context)
        if pair is not None:
            pairs.add(pair)
    pair = _valid_pair(
        detail.get("chapter"),
        detail.get("topic") or detail.get("knowledge_point"),
        context,
    )
    if pair is not None:
        pairs.add(pair)
    if len(pairs) == 1:
        chapter, topic = next(iter(pairs))
        return {"status": "classified", "chapter": chapter, "topic": topic}
    fingerprints = {
        str(attempt.question_fingerprint or "").strip() for attempt in attempts
    } - {""}
    pending = bool(fingerprints) and len(pairs) == 0 and db.query(
        KnowledgeEventClassificationJob.id,
    ).filter(
        KnowledgeEventClassificationJob.question_fingerprint.in_(fingerprints),
        KnowledgeEventClassificationJob.status.in_((
            STATUS_PENDING,
            STATUS_RETRY_WAITING,
            STATUS_RUNNING,
        )),
    ).first() is not None
    return {"status": STATUS_PENDING if pending else "failed", "chapter": "", "topic": ""}


def mark_question_grading_classified(question: QuestionBank) -> None:
    """Record a reused grading label as a model suggestion on a new bank row."""
    question.classification_status = "suggested"
    question.classification_source = "grading"
    question.classification_confidence = "medium"
    question.classification_version = str(question.catalog_version or "")[:64]
    question.classification_error = ""
    question.classified_at = _now()


def reconcile_classification_jobs_once(limit: int = 500) -> int:
    """Create missing jobs and reconcile rows manually classified by a teacher."""
    db = SessionLocal()
    try:
        now = _now()
        stage_rows = db.query(QuestionBank).filter(
            or_(QuestionBank.education_stage != "", QuestionBank.catalog_version != ""),
            QuestionBank.classification_status.in_((STATUS_PENDING, "", "confirmed", "suggested")),
            func.coalesce(QuestionBank.knowledge_point_id, "") == "",
        ).order_by(QuestionBank.id.asc()).limit(max(1, int(limit))).all()
        for question in stage_rows:
            try:
                context = _record_context(question)
                if not _question_is_unclassified(question):
                    point = get_knowledge_point(context["education_stage"], question.chapter, question.topic)
                    if point is None:
                        raise ValueError("已有知识点不属于冻结学段目录，请人工确认")
                    question.knowledge_point_id = point["id"]
            except ValueError as exc:
                question.classification_status = STATUS_MANUAL
                question.classification_error = str(exc)
                for job in db.query(QuestionClassificationJob).filter(QuestionClassificationJob.question_id == question.id).all():
                    job.status = STATUS_MANUAL
                    job.last_error = str(exc)
                    job.last_error_category = "catalog_context"
                    job.locked_until = None
                    job.worker_id = ""
                    job.completed_at = job.updated_at = now
        db.flush()
        unclassified_filter = or_(
            _column_is_unclassified(QuestionBank.chapter),
            _column_is_unclassified(QuestionBank.topic),
        )
        # Older imports used non-empty placeholders such as ``未知`` and were
        # therefore incorrectly marked confirmed. Reopen only those false
        # terminal states; genuine failed/manual rows keep their review state.
        db.query(QuestionBank).filter(
            unclassified_filter,
            QuestionBank.classification_status.in_((
                "",
                STATUS_PENDING,
                "confirmed",
                "suggested",
            )),
        ).update(
            {
                QuestionBank.classification_status: STATUS_PENDING,
                QuestionBank.classification_source: "",
                QuestionBank.classification_confidence: "",
                QuestionBank.classification_error: "",
                QuestionBank.classified_at: None,
            },
            synchronize_session=False,
        )
        # A teacher/import may have supplied both fields while the row still
        # carries the default pending state.  Treat that as an explicit answer.
        db.query(QuestionBank).filter(
            QuestionBank.classification_status.in_((STATUS_PENDING, "")),
            ~_column_is_unclassified(QuestionBank.chapter),
            ~_column_is_unclassified(QuestionBank.topic),
        ).update(
            {
                QuestionBank.classification_status: "confirmed",
                QuestionBank.classification_source: "import",
                QuestionBank.classification_confidence: "high",
                QuestionBank.classification_error: "",
                QuestionBank.classified_at: now,
            },
            synchronize_session=False,
        )

        # Rows that no longer need classification should not leave a running
        # job behind after a teacher edits them in the bank page.
        completed_rows = (
            db.query(QuestionClassificationJob)
            .join(QuestionBank, QuestionBank.id == QuestionClassificationJob.question_id)
            .filter(
                QuestionClassificationJob.status.in_((
                    STATUS_PENDING,
                    STATUS_RETRY_WAITING,
                    STATUS_RUNNING,
                )),
                ~_column_is_unclassified(QuestionBank.chapter),
                ~_column_is_unclassified(QuestionBank.topic),
            )
            .all()
        )
        for job in completed_rows:
            job.status = STATUS_COMPLETED
            job.locked_until = None
            job.worker_id = ""
            job.completed_at = now
            job.updated_at = now

        # A placeholder row may already own a completed job from the previous
        # non-empty-string rule. Reopen that job instead of creating a duplicate.
        stale_completed_rows = (
            db.query(QuestionClassificationJob)
            .join(QuestionBank, QuestionBank.id == QuestionClassificationJob.question_id)
            .filter(
                QuestionClassificationJob.status == STATUS_COMPLETED,
                QuestionBank.classification_status == STATUS_PENDING,
                or_(
                    _column_is_unclassified(QuestionBank.chapter),
                    _column_is_unclassified(QuestionBank.topic),
                ),
            )
            .all()
        )
        for job in stale_completed_rows:
            job.status = STATUS_PENDING
            job.attempts = 0
            job.next_attempt_at = now
            job.locked_until = None
            job.worker_id = ""
            job.last_error = ""
            job.last_error_category = ""
            job.completed_at = None
            job.updated_at = now

        # SQLite's INSERT OR IGNORE makes discovery safe when several web
        # workers run the reconciliation pass at the same time.
        missing = (
            db.query(QuestionBank.id)
            .outerjoin(
                QuestionClassificationJob,
                QuestionClassificationJob.question_id == QuestionBank.id,
            )
            .filter(
                QuestionClassificationJob.id.is_(None),
                QuestionBank.classification_status.in_((STATUS_PENDING, "")),
                or_(
                    _column_is_unclassified(QuestionBank.chapter),
                    _column_is_unclassified(QuestionBank.topic),
                ),
            )
            .order_by(QuestionBank.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        for (question_id,) in missing:
            db.execute(
                insert(QuestionClassificationJob)
                .prefix_with("OR IGNORE")
                .values(
                    question_id=int(question_id),
                    status=STATUS_PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    worker_id="",
                    last_error="",
                    last_error_category="",
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()
        return len(missing)
    except Exception:
        db.rollback()
        logger.exception("question classification reconciliation failed")
        return 0
    finally:
        db.close()


def reconcile_knowledge_event_classification_jobs_once(limit: int = 500) -> int:
    """Discover archived wrong questions and queue one job per fingerprint."""
    db = SessionLocal()
    try:
        now = _now()
        active_jobs = db.query(KnowledgeEventClassificationJob).filter(
            KnowledgeEventClassificationJob.status.in_((
                STATUS_PENDING,
                STATUS_RETRY_WAITING,
                STATUS_RUNNING,
                STATUS_MANUAL,
            )),
        ).order_by(KnowledgeEventClassificationJob.id.asc()).limit(
            max(1, int(limit))
        ).all()
        for job in active_jobs:
            attempts = db.query(QuestionAttemptEvent).filter(
                QuestionAttemptEvent.question_fingerprint == job.question_fingerprint,
            ).all()
            trusted = _trusted_attempt_group_knowledge(db, attempts)
            if trusted is not None:
                _propagate_fingerprint_knowledge(
                    db,
                    question_fingerprint=job.question_fingerprint,
                    chapter=trusted[0],
                    topic=trusted[1],
                    classification_source="existing",
                    classification_confidence="high",
                )

        candidates = (
            db.query(QuestionAttemptEvent)
            .outerjoin(
                KnowledgeEventClassificationJob,
                KnowledgeEventClassificationJob.question_fingerprint
                == QuestionAttemptEvent.question_fingerprint,
            )
            .filter(
                KnowledgeEventClassificationJob.id.is_(None),
                QuestionAttemptEvent.analytics_included.is_(True),
                QuestionAttemptEvent.final_result.in_(("wrong", "partial", "blank")),
                func.trim(QuestionAttemptEvent.question_fingerprint) != "",
            )
            .order_by(QuestionAttemptEvent.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        created = 0
        seen: set[str] = set()
        for candidate in candidates:
            fingerprint = str(candidate.question_fingerprint or "").strip()
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            attempts = db.query(QuestionAttemptEvent).filter(
                QuestionAttemptEvent.question_fingerprint == fingerprint,
            ).order_by(QuestionAttemptEvent.id.asc()).all()
            trusted = _trusted_attempt_group_knowledge(db, attempts)
            status = STATUS_COMPLETED if trusted is not None else STATUS_PENDING
            inserted = db.execute(
                insert(KnowledgeEventClassificationJob)
                .prefix_with("OR IGNORE")
                .values(
                    attempt_id=int(candidate.id),
                    question_fingerprint=fingerprint,
                    status=status,
                    attempts=0,
                    next_attempt_at=now,
                    worker_id="",
                    last_error="",
                    last_error_category="",
                    created_at=now,
                    updated_at=now,
                    completed_at=now if trusted is not None else None,
                )
            )
            created += max(0, int(inserted.rowcount or 0))
            if trusted is not None:
                _propagate_fingerprint_knowledge(
                    db,
                    question_fingerprint=fingerprint,
                    chapter=trusted[0],
                    topic=trusted[1],
                    classification_source="existing",
                    classification_confidence="high",
                )
        db.commit()
        return created
    except Exception:
        db.rollback()
        logger.exception("historical knowledge classification reconciliation failed")
        return 0
    finally:
        db.close()


def _recover_expired_jobs(db, now: datetime) -> None:
    """Return crashed worker leases to the retry schedule."""
    expired = (
        db.query(QuestionClassificationJob)
        .filter(
            QuestionClassificationJob.status == STATUS_RUNNING,
            or_(
                QuestionClassificationJob.locked_until.is_(None),
                QuestionClassificationJob.locked_until <= now,
            ),
        )
        .all()
    )
    for job in expired:
        if int(job.attempts or 0) < MAX_ATTEMPTS:
            job.status = STATUS_RETRY_WAITING
            job.next_attempt_at = now + timedelta(seconds=_retry_delay(job.attempts))
        else:
            job.status = STATUS_MANUAL
            question = db.get(QuestionBank, job.question_id)
            if question is not None:
                question.classification_status = "failed"
                question.classification_source = "model"
                question.classification_error = "后台分类任务中断次数过多，请人工确认"
        job.locked_until = None
        job.worker_id = ""
        job.updated_at = now


def _recover_expired_knowledge_jobs(db, now: datetime) -> None:
    expired = db.query(KnowledgeEventClassificationJob).filter(
        KnowledgeEventClassificationJob.status == STATUS_RUNNING,
        or_(
            KnowledgeEventClassificationJob.locked_until.is_(None),
            KnowledgeEventClassificationJob.locked_until <= now,
        ),
    ).all()
    for job in expired:
        if int(job.attempts or 0) < MAX_ATTEMPTS:
            job.status = STATUS_RETRY_WAITING
            job.next_attempt_at = now + timedelta(seconds=_retry_delay(job.attempts))
        else:
            job.status = STATUS_MANUAL
            job.completed_at = now
            job.last_error = "后台分类任务中断次数过多，请人工确认"
            job.last_error_category = "interrupted"
        job.locked_until = None
        job.worker_id = ""
        job.updated_at = now


def claim_classification_batch(
    worker_id: str,
    batch_size: int = KNOWLEDGE_CLASSIFIER_BATCH_SIZE,
) -> list[dict]:
    """Atomically claim one batch, while allowing only one active batch globally."""
    db = SessionLocal()
    try:
        now = _now()
        _recover_expired_jobs(db, now)
        _recover_expired_knowledge_jobs(db, now)
        lease_until = now + timedelta(seconds=float(KNOWLEDGE_CLASSIFIER_LEASE_S))

        running = aliased(QuestionClassificationJob)
        historical_running = aliased(KnowledgeEventClassificationJob)
        # Keep the global-running probe independent from the candidate row.
        # Without an explicit FROM/correlation boundary SQLAlchemy can
        # auto-correlate the aliased table out of the EXISTS subquery on
        # SQLite, making the single-lane guard depend on the outer row.
        active_running = (
            select(1)
            .select_from(running)
            .where(
                running.status == STATUS_RUNNING,
                running.locked_until.isnot(None),
                running.locked_until > now,
            )
            .correlate(None)
            .exists()
        )
        active_historical_running = (
            select(1)
            .select_from(historical_running)
            .where(
                historical_running.status == STATUS_RUNNING,
                historical_running.locked_until.isnot(None),
                historical_running.locked_until > now,
            )
            .correlate(None)
            .exists()
        )
        candidate_id = (
            select(QuestionClassificationJob.id)
            .where(
                QuestionClassificationJob.status.in_((
                    STATUS_PENDING,
                    STATUS_RETRY_WAITING,
                )),
                QuestionClassificationJob.next_attempt_at <= now,
                ~active_running,
                ~active_historical_running,
            )
            .order_by(
                QuestionClassificationJob.next_attempt_at.asc(),
                QuestionClassificationJob.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        claimed = db.execute(
            update(QuestionClassificationJob)
            .where(QuestionClassificationJob.id == candidate_id)
            .values(
                status=STATUS_RUNNING,
                attempts=QuestionClassificationJob.attempts + 1,
                locked_until=lease_until,
                worker_id=worker_id,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return []

        # The first update is in the same transaction as the remaining claims;
        # competing workers cannot observe a half-claimed batch.
        first_job = (
            db.query(QuestionClassificationJob)
            .filter(
                QuestionClassificationJob.status == STATUS_RUNNING,
                QuestionClassificationJob.worker_id == worker_id,
            )
            .order_by(QuestionClassificationJob.id.desc())
            .first()
        )
        if first_job is None:
            db.rollback()
            return []
        selected = [first_job]
        remaining = max(0, min(int(batch_size), 50) - 1)
        if remaining:
            more = (
                db.query(QuestionClassificationJob)
                .filter(
                    QuestionClassificationJob.status.in_((
                        STATUS_PENDING,
                        STATUS_RETRY_WAITING,
                    )),
                    QuestionClassificationJob.next_attempt_at <= now,
                )
                .order_by(
                    QuestionClassificationJob.next_attempt_at.asc(),
                    QuestionClassificationJob.id.asc(),
                )
                .limit(remaining)
                .all()
            )
            for job in more:
                job.status = STATUS_RUNNING
                job.attempts = int(job.attempts or 0) + 1
                job.locked_until = lease_until
                job.worker_id = worker_id
                job.updated_at = now
                selected.append(job)
        db.commit()

        question_ids = [int(job.question_id) for job in selected]
        questions = (
            db.query(QuestionBank)
            .filter(QuestionBank.id.in_(question_ids))
            .all()
        )
        by_id = {int(question.id): question for question in questions}
        return [
            {
                "job_id": int(job.id),
                "question_id": int(job.question_id),
                "attempts": int(job.attempts or 0),
                "worker_id": worker_id,
                "question": str(by_id[job.question_id].question or ""),
                "chapter": str(by_id[job.question_id].chapter or ""),
                "topic": str(by_id[job.question_id].topic or ""),
                "education_stage": str(by_id[job.question_id].education_stage or ""),
                "catalog_version": str(by_id[job.question_id].catalog_version or ""),
            }
            for job in selected
            if int(job.question_id) in by_id
        ]
    except Exception:
        db.rollback()
        logger.exception("question classification claim failed")
        return []
    finally:
        db.close()


def claim_knowledge_event_classification(
    worker_id: str,
    batch_size: int = 1,
) -> list[dict]:
    """Atomically claim archived questions on the shared classifier serial lane."""
    db = SessionLocal()
    try:
        now = _now()
        _recover_expired_jobs(db, now)
        _recover_expired_knowledge_jobs(db, now)
        lease_until = now + timedelta(seconds=float(KNOWLEDGE_CLASSIFIER_LEASE_S))

        bank_running = aliased(QuestionClassificationJob)
        historical_running = aliased(KnowledgeEventClassificationJob)
        active_bank = (
            select(1)
            .select_from(bank_running)
            .where(
                bank_running.status == STATUS_RUNNING,
                bank_running.locked_until.isnot(None),
                bank_running.locked_until > now,
            )
            .correlate(None)
            .exists()
        )
        active_historical = (
            select(1)
            .select_from(historical_running)
            .where(
                historical_running.status == STATUS_RUNNING,
                historical_running.locked_until.isnot(None),
                historical_running.locked_until > now,
            )
            .correlate(None)
            .exists()
        )
        candidate_id = (
            select(KnowledgeEventClassificationJob.id)
            .where(
                KnowledgeEventClassificationJob.status.in_((
                    STATUS_PENDING,
                    STATUS_RETRY_WAITING,
                )),
                KnowledgeEventClassificationJob.next_attempt_at <= now,
                ~active_bank,
                ~active_historical,
            )
            .order_by(
                KnowledgeEventClassificationJob.next_attempt_at.asc(),
                KnowledgeEventClassificationJob.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        claimed = db.execute(
            update(KnowledgeEventClassificationJob)
            .where(KnowledgeEventClassificationJob.id == candidate_id)
            .values(
                status=STATUS_RUNNING,
                attempts=KnowledgeEventClassificationJob.attempts + 1,
                locked_until=lease_until,
                worker_id=worker_id,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return []
        job = db.query(KnowledgeEventClassificationJob).filter(
            KnowledgeEventClassificationJob.status == STATUS_RUNNING,
            KnowledgeEventClassificationJob.worker_id == worker_id,
        ).order_by(KnowledgeEventClassificationJob.id.desc()).first()
        if job is None:
            db.rollback()
            return []
        selected = [job]
        remaining = max(0, min(int(batch_size), 50) - 1)
        if remaining:
            more = db.query(KnowledgeEventClassificationJob).filter(
                KnowledgeEventClassificationJob.status.in_((STATUS_PENDING, STATUS_RETRY_WAITING)),
                KnowledgeEventClassificationJob.next_attempt_at <= now,
            ).order_by(KnowledgeEventClassificationJob.next_attempt_at.asc(),
                       KnowledgeEventClassificationJob.id.asc()).limit(remaining).all()
            for additional in more:
                additional.status = STATUS_RUNNING
                additional.attempts = int(additional.attempts or 0) + 1
                additional.locked_until = lease_until
                additional.worker_id = worker_id
                additional.updated_at = now
                selected.append(additional)
        items = []
        for selected_job in selected:
            attempt = db.get(QuestionAttemptEvent, int(selected_job.attempt_id))
            if attempt is None:
                db.delete(selected_job)
                continue
            item = {
                "job_id": int(selected_job.id), "attempt_id": int(attempt.id),
                "attempts": int(selected_job.attempts or 0), "worker_id": worker_id,
                "question_fingerprint": str(selected_job.question_fingerprint or ""),
                "question": _attempt_question_text(attempt),
            }
            snapshot = attempt.question_snapshot()
            item.update({key: snapshot[key] for key in ("teaching_context", "education_stage", "catalog_version") if key in snapshot})
            # This is a transient worker request, not a mutation of the
            # immutable question snapshot or its historical fingerprint.
            item["teaching_context"] = _attempt_context(attempt, db)
            items.append(item)
        db.commit()
        return items
    except Exception:
        db.rollback()
        logger.exception("historical knowledge classification claim failed")
        return []
    finally:
        db.close()


def _truncate_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc) or '上游服务异常'}"[:500]


def is_retryable_classification_error(exc: Exception) -> bool:
    from services.analyzer import KnowledgeClassifierConfigurationError
    from services.flash_grading import FlashGradingError

    if isinstance(exc, KnowledgeClassifierConfigurationError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(exc.response.status_code)
        return status == 408 or status == 429 or status >= 500
    if isinstance(exc, FlashGradingError) and isinstance(getattr(exc, "status_code", None), int):
        status = exc.status_code
        return status == 408 or status == 429 or status >= 500
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TransportError)):
        return True
    # A malformed upstream JSON envelope is worth trying again once the
    # provider has recovered, while local configuration errors are terminal.
    if isinstance(exc, ValueError):
        return True
    if isinstance(exc, RuntimeError):
        return "未配置" not in str(exc) and "配置" not in str(exc)
    return False


def finish_classification_batch(
    claimed: list[dict],
    results: list[dict],
) -> None:
    """Persist validated results and release the claimed jobs."""
    db = SessionLocal()
    try:
        now = _now()
        by_id = {
            str(item.get("id")): item
            for item in results
            if isinstance(item, dict) and item.get("id") is not None
        }
        for item in claimed:
            job = db.get(QuestionClassificationJob, int(item["job_id"]))
            question = db.get(QuestionBank, int(item["question_id"]))
            if job is None:
                continue
            # A lease may have expired, been reclaimed, or been cancelled by
            # a teacher while the provider was still responding.  Never let
            # that stale response overwrite the newer attempt.
            if (
                job.status != STATUS_RUNNING
                or str(job.worker_id or "") != str(item.get("worker_id") or "")
                or int(job.attempts or 0) != int(item.get("attempts") or 0)
            ):
                continue
            if question is None:
                job.status = STATUS_COMPLETED
                job.completed_at = now
                job.locked_until = None
                job.worker_id = ""
                job.updated_at = now
                continue

            # A teacher may have edited the row while the provider call was in
            # flight.  Never overwrite an explicit teacher classification.
            try:
                context = _record_context(question)
                if context != _claimed_context(item):
                    raise ValueError("分类期间记录的学段或目录快照发生变化")
            except ValueError as exc:
                question.classification_status = job.status = STATUS_MANUAL
                question.classification_error = job.last_error = str(exc)
                job.last_error_category = "catalog_context"
                job.completed_at = job.updated_at = now
                job.locked_until = None
                job.worker_id = ""
                continue
            if not _question_is_unclassified(question):
                question.classification_status = "confirmed"
                question.classification_source = "teacher"
                question.classification_confidence = "high"
                question.classification_error = ""
                question.classified_at = now
                job.status = STATUS_COMPLETED
                job.completed_at = now
            else:
                result = by_id.get(str(item["question_id"])) or {}
                if result.get("status") == "suggested" and _valid_pair(result.get("chapter"), result.get("topic"), context):
                    question.chapter = str(result["chapter"]).strip()
                    question.topic = str(result["topic"]).strip()
                    question.classification_status = "suggested"
                    question.classification_source = "model"
                    question.classification_confidence = str(
                        result.get("confidence") or "medium"
                    )[:10]
                    question.classification_version = str(
                        result.get("classification_version") or ""
                    )[:64]
                    question.classification_error = ""
                    question.classified_at = now
                    job.status = STATUS_COMPLETED
                    job.completed_at = now
                elif (
                    result.get("status") == "rejected"
                    and int(job.attempts or 0) < MAX_ATTEMPTS
                ):
                    # The model answered outside the catalogue. Ask again on
                    # the normal schedule before handing it to the teacher.
                    question.classification_status = STATUS_PENDING
                    question.classification_error = ""
                    job.status = STATUS_RETRY_WAITING
                    job.next_attempt_at = now + timedelta(
                        seconds=_retry_delay(job.attempts)
                    )
                    job.last_error = str(result.get("error") or "")[:500]
                    job.last_error_category = "rejected"
                else:
                    question.classification_status = STATUS_MANUAL
                    question.classification_source = "model"
                    question.classification_confidence = "low"
                    question.classification_error = str(
                        REJECTED_ERROR if result.get("status") == "rejected"
                        else result.get("error") or "模型无法确定，请人工确认"
                    )[:500]
                    job.status = STATUS_MANUAL
                    job.completed_at = now
                    job.last_error = question.classification_error
                    job.last_error_category = "ambiguous"
            pair = _valid_pair(question.chapter, question.topic, context)
            if pair and context:
                question.knowledge_point_id = get_knowledge_point(context["education_stage"], *pair)["id"]
            elif context and job.status == STATUS_COMPLETED:
                question.classification_status = job.status = STATUS_MANUAL
                question.classification_error = job.last_error = "已有知识点不属于冻结学段目录，请人工确认"
                job.last_error_category = "catalog_mismatch"
            if pair is not None and job.status == STATUS_COMPLETED:
                fingerprints = db.query(
                    QuestionAttemptEvent.question_fingerprint,
                ).filter(
                    QuestionAttemptEvent.question_bank_id == question.id,
                    func.trim(QuestionAttemptEvent.question_fingerprint) != "",
                ).distinct().all()
                for (fingerprint,) in fingerprints:
                    _propagate_fingerprint_knowledge(
                        db,
                        question_fingerprint=str(fingerprint),
                        chapter=pair[0],
                        topic=pair[1],
                        classification_source=str(
                            question.classification_source or "model"
                        ),
                        classification_confidence=str(
                            question.classification_confidence or "medium"
                        ),
                        classification_version=str(
                            question.classification_version or ""
                        ),
                    )
            job.locked_until = None
            job.worker_id = ""
            job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("question classification result persistence failed")
    finally:
        db.close()


def finish_knowledge_event_classification(
    claimed: list[dict],
    results: list[dict],
) -> None:
    """Persist a historical classification and release its fingerprint job."""
    db = SessionLocal()
    try:
        now = _now()
        by_id = {
            str(item.get("id")): item
            for item in results
            if isinstance(item, dict) and item.get("id") is not None
        }
        for item in claimed:
            job = db.get(KnowledgeEventClassificationJob, int(item["job_id"]))
            if job is None or (
                job.status != STATUS_RUNNING
                or str(job.worker_id or "") != str(item.get("worker_id") or "")
                or int(job.attempts or 0) != int(item.get("attempts") or 0)
            ):
                continue
            attempts = db.query(QuestionAttemptEvent).filter(
                QuestionAttemptEvent.question_fingerprint == job.question_fingerprint,
            ).all()
            try:
                context = _claimed_context(item)
                if any(_attempt_context(attempt, db) != context for attempt in attempts):
                    raise ValueError("同题归档包含冲突的学段快照")
            except ValueError as exc:
                job.status = STATUS_MANUAL
                job.last_error = str(exc)
                job.last_error_category = "catalog_context"
                job.completed_at = job.updated_at = now
                job.locked_until = None
                job.worker_id = ""
                continue
            trusted = _trusted_attempt_group_knowledge(db, attempts)
            result = by_id.get(str(item["attempt_id"])) or {}
            if trusted is not None:
                pair = trusted
                source = "existing"
                confidence = "high"
                version = ""
            elif (
                result.get("status") == "suggested"
                and result.get("chapter")
                and result.get("topic")
            ):
                pair = _valid_pair(result.get("chapter"), result.get("topic"), context)
                source = "model"
                confidence = str(result.get("confidence") or "medium")
                version = str(result.get("classification_version") or "")
            else:
                pair = None
                source = "model"
                confidence = "low"
                version = ""

            if pair is not None:
                _propagate_fingerprint_knowledge(
                    db,
                    question_fingerprint=job.question_fingerprint,
                    chapter=pair[0],
                    topic=pair[1],
                    classification_source=source,
                    classification_confidence=confidence,
                    classification_version=version,
                )
                job.status = STATUS_COMPLETED
                job.completed_at = now
                job.last_error = ""
                job.last_error_category = ""
            elif (
                result.get("status") == "rejected"
                and int(job.attempts or 0) < MAX_ATTEMPTS
            ):
                job.status = STATUS_RETRY_WAITING
                job.next_attempt_at = now + timedelta(
                    seconds=_retry_delay(job.attempts)
                )
                job.last_error = str(result.get("error") or "")[:500]
                job.last_error_category = "rejected"
            else:
                job.status = STATUS_MANUAL
                job.completed_at = now
                job.last_error = str(
                    REJECTED_ERROR if result.get("status") == "rejected"
                    else result.get("error") or "模型无法确定知识点，请人工确认"
                )[:500]
                job.last_error_category = "ambiguous"
            job.locked_until = None
            job.worker_id = ""
            job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("historical knowledge classification persistence failed")
    finally:
        db.close()


def fail_knowledge_event_classification(
    claimed: list[dict],
    exc: Exception,
) -> None:
    """Retry or stop one archived-question classification."""
    db = SessionLocal()
    try:
        now = _now()
        retryable = is_retryable_classification_error(exc)
        error = _truncate_error(exc)
        for item in claimed:
            job = db.get(KnowledgeEventClassificationJob, int(item["job_id"]))
            if job is None or (
                job.status != STATUS_RUNNING
                or str(job.worker_id or "") != str(item.get("worker_id") or "")
                or int(job.attempts or 0) != int(item.get("attempts") or 0)
            ):
                continue
            attempts = int(job.attempts or 0)
            if retryable and attempts < MAX_ATTEMPTS:
                job.status = STATUS_RETRY_WAITING
                job.next_attempt_at = now + timedelta(
                    seconds=_retry_delay(attempts)
                )
                job.last_error_category = "upstream"
            else:
                job.status = STATUS_MANUAL
                job.completed_at = now
                job.last_error_category = "permanent"
            job.last_error = error
            job.locked_until = None
            job.worker_id = ""
            job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("historical knowledge classification failure persistence failed")
    finally:
        db.close()


def fail_classification_batch(
    claimed: list[dict],
    exc: Exception,
) -> None:
    """Schedule a stepped retry or move the batch to manual review."""
    db = SessionLocal()
    try:
        now = _now()
        retryable = is_retryable_classification_error(exc)
        error = _truncate_error(exc)
        category = "upstream" if retryable else "permanent"
        for item in claimed:
            job = db.get(QuestionClassificationJob, int(item["job_id"]))
            question = db.get(QuestionBank, int(item["question_id"]))
            if job is None:
                continue
            # Do not reschedule a job that has already been reclaimed or
            # explicitly cancelled by a teacher.
            if (
                job.status != STATUS_RUNNING
                or str(job.worker_id or "") != str(item.get("worker_id") or "")
                or int(job.attempts or 0) != int(item.get("attempts") or 0)
            ):
                continue
            attempts = int(job.attempts or item.get("attempts") or 0)
            if retryable and attempts < MAX_ATTEMPTS:
                delay = _retry_delay(attempts)
                job.status = STATUS_RETRY_WAITING
                job.next_attempt_at = now + timedelta(seconds=delay)
                logger.warning(
                    "question classification upstream failure; retry in %ss "
                    "attempt=%s/%s job=%s",
                    delay,
                    attempts,
                    MAX_ATTEMPTS,
                    job.id,
                )
            else:
                job.status = STATUS_MANUAL
                job.completed_at = now
                logger.error(
                    "question classification moved to manual review attempt=%s job=%s",
                    attempts,
                    job.id,
                )
                if question is not None:
                    question.classification_status = "failed"
                    question.classification_source = "model"
                    question.classification_confidence = "low"
                    question.classification_error = (
                        "上游分类服务异常，已停止自动重试，请人工确认：" + error
                    )[:500]
            job.last_error = error
            job.last_error_category = category
            job.locked_until = None
            job.worker_id = ""
            job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("question classification failure persistence failed")
    finally:
        db.close()


def mark_question_classified(db, question: QuestionBank, *, source: str = "teacher") -> None:
    """Mark a manually supplied chapter/topic and cancel its queued job."""
    context = _record_context(question)
    if context:
        point = get_knowledge_point(context["education_stage"], question.chapter, question.topic)
        if point is None:
            raise ValueError("知识点不属于冻结学段目录")
        question.knowledge_point_id = point["id"]
    now = _now()
    question.classification_status = "confirmed"
    question.classification_source = source[:20]
    question.classification_confidence = "high"
    question.classification_error = ""
    question.classified_at = now
    job = (
        db.query(QuestionClassificationJob)
        .filter(QuestionClassificationJob.question_id == question.id)
        .first()
    )
    if job is not None:
        job.status = STATUS_COMPLETED
        job.locked_until = None
        job.worker_id = ""
        job.completed_at = now
        job.updated_at = now
    pair = _knowledge_pair(question.chapter, question.topic)
    if pair is not None:
        fingerprints = db.query(
            QuestionAttemptEvent.question_fingerprint,
        ).filter(
            QuestionAttemptEvent.question_bank_id == question.id,
            func.trim(QuestionAttemptEvent.question_fingerprint) != "",
        ).distinct().all()
        for (fingerprint,) in fingerprints:
            _propagate_fingerprint_knowledge(
                db,
                question_fingerprint=str(fingerprint),
                chapter=pair[0],
                topic=pair[1],
                classification_source=source,
                classification_confidence="high",
            )


def queue_question_classification(db, question: QuestionBank) -> None:
    """Reset a cleared classification and make it eligible immediately."""
    _record_context(question)
    question.knowledge_point_id = ""
    now = _now()
    question.classification_status = STATUS_PENDING
    question.classification_source = ""
    question.classification_confidence = ""
    question.classification_version = ""
    question.classification_error = ""
    question.classified_at = None
    job = (
        db.query(QuestionClassificationJob)
        .filter(QuestionClassificationJob.question_id == question.id)
        .first()
    )
    if job is None:
        db.add(
            QuestionClassificationJob(
                question_id=int(question.id),
                status=STATUS_PENDING,
                attempts=0,
                next_attempt_at=now,
                worker_id="",
                last_error="",
                last_error_category="",
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        return
    # Clearing a row invalidates any in-flight result.  The attempt counter is
    # reset so the new classification receives the full retry budget.
    job.status = STATUS_PENDING
    job.attempts = 0
    job.next_attempt_at = now
    job.locked_until = None
    job.worker_id = ""
    job.last_error = ""
    job.last_error_category = ""
    job.completed_at = None
    job.updated_at = now


def get_classification_status(db, teacher_id: int | None = None) -> dict:
    """Return counts suitable for a quiet progress indicator in the UI."""
    from services.bank_scope import visible_filter

    query = db.query(QuestionBank).filter(visible_filter(teacher_id)) if teacher_id is not None else db.query(QuestionBank)
    rows = query.with_entities(
        QuestionBank.classification_status,
        func.count(QuestionBank.id),
    ).group_by(QuestionBank.classification_status).all()
    counts = {str(status or STATUS_PENDING): int(count) for status, count in rows}
    history_query = db.query(
        KnowledgeEventClassificationJob.status,
        func.count(KnowledgeEventClassificationJob.id),
    ).join(
        QuestionAttemptEvent,
        QuestionAttemptEvent.id == KnowledgeEventClassificationJob.attempt_id,
    )
    unclassified_attempt_query = db.query(
        func.count(QuestionAttemptEvent.id),
    ).join(
        KnowledgeEventClassificationJob,
        KnowledgeEventClassificationJob.question_fingerprint
        == QuestionAttemptEvent.question_fingerprint,
    ).filter(
        QuestionAttemptEvent.analytics_included.is_(True),
        QuestionAttemptEvent.final_result.in_(("wrong", "partial", "blank")),
        KnowledgeEventClassificationJob.status != STATUS_COMPLETED,
    )
    if teacher_id is not None:
        history_query = history_query.filter(
            QuestionAttemptEvent.teacher_id == int(teacher_id),
        )
        unclassified_attempt_query = unclassified_attempt_query.filter(
            QuestionAttemptEvent.teacher_id == int(teacher_id),
        )
    history_rows = history_query.group_by(
        KnowledgeEventClassificationJob.status,
    ).all()
    history_counts = {
        str(status or STATUS_PENDING): int(count)
        for status, count in history_rows
    }
    unclassified_attempts = int(unclassified_attempt_query.scalar() or 0)
    history_pending = sum(
        history_counts.get(status, 0)
        for status in (STATUS_PENDING, STATUS_RETRY_WAITING, STATUS_RUNNING)
    )
    history_completed = history_counts.get(STATUS_COMPLETED, 0)
    history_manual = history_counts.get(STATUS_MANUAL, 0)
    bank_total = sum(counts.values())
    return {
        "total": bank_total + sum(history_counts.values()),
        "confirmed": counts.get("confirmed", 0),
        "suggested": counts.get("suggested", 0) + history_completed,
        "pending": counts.get("pending", 0) + history_pending,
        "failed": counts.get("failed", 0) + history_manual,
        "manual": counts.get("manual", 0) + history_manual,
        "question_bank": {
            "total": bank_total,
            **counts,
        },
        "historical": {
            "total": sum(history_counts.values()),
            "completed": history_completed,
            "pending": history_pending,
            "manual": history_manual,
            "unclassified_attempts": unclassified_attempts,
        },
    }


async def question_classification_worker_loop(stop_event: asyncio.Event) -> None:
    """Run the durable classifier until application shutdown."""
    worker_id = _worker_id()
    prefer_historical = False
    while not stop_event.is_set():
        try:
            if not classifier_worker_configured():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=float(KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S),
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            await asyncio.to_thread(reconcile_classification_jobs_once)
            await asyncio.to_thread(
                reconcile_knowledge_event_classification_jobs_once
            )
            # The global lease retains one classifier lane. Claimed items are
            # partitioned by frozen catalogue before a whole-group model call.
            historical_claimed: list[dict] = []
            claimed: list[dict] = []
            if prefer_historical:
                historical_claimed = await asyncio.to_thread(
                    claim_knowledge_event_classification,
                    worker_id,
                    KNOWLEDGE_CLASSIFIER_BATCH_SIZE,
                )
            if not historical_claimed:
                claimed = await asyncio.to_thread(
                    claim_classification_batch,
                    worker_id,
                    KNOWLEDGE_CLASSIFIER_BATCH_SIZE,
                )
            if not claimed and not historical_claimed and not prefer_historical:
                historical_claimed = await asyncio.to_thread(
                    claim_knowledge_event_classification,
                    worker_id,
                    KNOWLEDGE_CLASSIFIER_BATCH_SIZE,
                )
            if claimed or historical_claimed:
                prefer_historical = not prefer_historical
            if claimed:
                active = [
                    item for item in claimed
                    if str(item.get("question") or "").strip()
                    and (
                        str(item.get("chapter") or "").strip() in UNCLASSIFIED_KNOWLEDGE_VALUES
                        or str(item.get("topic") or "").strip() in UNCLASSIFIED_KNOWLEDGE_VALUES
                    )
                ]
                skipped = [item for item in claimed if item not in active]
                if skipped:
                    await asyncio.to_thread(finish_classification_batch, skipped, [])
                if active:
                    try:
                        results = await _classify_stage_groups(active, "question_id")
                    except Exception as exc:
                        await asyncio.to_thread(fail_classification_batch, active, exc)
                    else:
                        await asyncio.to_thread(finish_classification_batch, active, results)
                elif not skipped:
                    # Defensive branch for a future claim filter change; in
                    # the current flow an all-skipped batch was already
                    # finalized above.
                    await asyncio.to_thread(finish_classification_batch, claimed, [])
                continue
            if historical_claimed:
                active = [item for item in historical_claimed if str(item.get("question") or "").strip()]
                skipped = [item for item in historical_claimed if item not in active]
                if skipped:
                    await asyncio.to_thread(
                        finish_knowledge_event_classification,
                        skipped,
                        [],
                    )
                if not active:
                    continue
                try:
                    results = await _classify_stage_groups(active, "attempt_id")
                except Exception as exc:
                    await asyncio.to_thread(
                        fail_knowledge_event_classification,
                        active,
                        exc,
                    )
                else:
                    await asyncio.to_thread(
                        finish_knowledge_event_classification,
                        active,
                        results,
                    )
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("question classification worker iteration failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=float(KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S)
            )
        except asyncio.TimeoutError:
            pass
