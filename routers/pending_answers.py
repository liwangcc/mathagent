"""待入库答案 — 从批改结果中提取LLM生成的标准答案，等老师确认后入库。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_
import hashlib
import json

from database import get_db
from models_user import User
from routers.auth import _require_user
from models import GradingResult, PendingAnswerDismissal, QuestionBank, Student
from services.teaching_context import record_teaching_fields, use_teaching_context, validate_teaching_context
from services.correction_tracking import grading_result_teaching_context
from services.answer_import import _validate_import_scope
from services.bank_scope import visible_filter
from services.question_classification import (
    UNCLASSIFIED_KNOWLEDGE_VALUES,
    grading_knowledge_for_detail,
    mark_question_grading_classified,
)

router = APIRouter()


class ConfirmAnswer(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000)
    answer: str = Field(..., min_length=1, max_length=5000)
    chapter: str | None = Field(None, max_length=100)
    topic: str | None = Field(None, max_length=100)
    grading_id: int | None = Field(None, ge=1)
    detail_index: int | None = Field(None, ge=0)


class ConfirmRequest(BaseModel):
    answers: list[ConfirmAnswer] = Field(..., min_length=1, max_length=100)


def _source_teaching_context(row, item, raw):
    # Empty legacy JSON is preserved during the explicitly approved adoption.
    # Read the adopted archive metadata without rewriting the frozen contract
    # or deriving history from the teacher's current profile.
    contexts = [validate_teaching_context(value["teaching_context"])
                for value in (item, raw)
                if isinstance(value, dict) and "teaching_context" in value]
    contexts = [context for context in contexts if context]
    if row.education_stage:
        contexts.append(grading_result_teaching_context(row))
    if contexts and any(context != contexts[0] for context in contexts[1:]):
        raise HTTPException(409, detail="待入库来源学段与归档记录冲突，请先核对来源")
    return contexts[0] if contexts else {}


def _dismissal_key(question: str, context: dict) -> tuple[str, str]:
    """Stable (question, teaching scope) identity of one pending suggestion."""
    question_hash = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()
    context_key = hashlib.sha256(json.dumps(context or {}, sort_keys=True).encode("utf-8")).hexdigest()
    return question_hash, context_key


def _dismissed_keys(db: Session, user: User) -> set[tuple[str, str]]:
    return {
        (row.question_hash, row.context_key)
        for row in db.query(PendingAnswerDismissal).filter(PendingAnswerDismissal.teacher_id == user.id).all()
    }


def _pending_sources(db: Session, user: User):
    rows = db.query(GradingResult).join(Student, GradingResult.student_id == Student.id).filter(
        Student.teacher_id == user.id, GradingResult.deleted_at.is_(None),
    ).all()
    dismissed = _dismissed_keys(db, user)
    for row in rows:
        try:
            details = json.loads(row.grading_details or "[]")
            raw = json.loads(row.ocr_raw_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(details, list):
            continue
        for index, item in enumerate(details):
            if not isinstance(item, dict) or not str(item.get("model_answer") or "").strip() or not str(item.get("question") or "").strip():
                continue
            context = _source_teaching_context(row, item, raw)
            if dismissed and _dismissal_key(item["question"], context) in dismissed:
                continue
            yield row.id, index, item, context


# Copies of one question share a grading label; bound the lookups per question.
_KNOWLEDGE_SOURCE_LIMIT = 10


def _grading_knowledge(db, sources) -> dict:
    """First grading label among a question's sources; no model call."""
    knowledge = {"status": "failed", "chapter": "", "topic": ""}
    for grading_id, index, item, context in sources[:_KNOWLEDGE_SOURCE_LIMIT]:
        found = grading_knowledge_for_detail(
            db, grading_result_id=grading_id, detail_index=index, detail=item, context=context,
        )
        if found["status"] == "classified":
            return found
        if found["status"] == "pending":
            knowledge = found
    return knowledge


def _bank_knowledge(ans, knowledge: dict) -> tuple[str, str, str]:
    """Return chapter, topic and who chose them: grading, teacher or nobody."""
    chapter, topic = (ans.chapter or "").strip(), (ans.topic or "").strip()
    supplied = chapter not in UNCLASSIFIED_KNOWLEDGE_VALUES and topic not in UNCLASSIFIED_KNOWLEDGE_VALUES
    reused = knowledge["status"] == "classified"
    if reused and (not supplied or (chapter, topic) == (knowledge["chapter"], knowledge["topic"])):
        return knowledge["chapter"], knowledge["topic"], "grading"
    return chapter, topic, "teacher" if supplied else ""


def _existing_pending_question(db, user, question, context):
    with use_teaching_context(context):
        query = db.query(QuestionBank).filter(visible_filter(user.id), QuestionBank.question == question)
    if not context:
        query = query.filter(or_(QuestionBank.education_stage == "", QuestionBank.education_stage.is_(None)))
    return query.first()


@router.get("/bank/pending-answers")
def get_pending_answers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """从批改结果中提取有model_answer但未入库的题目。"""
    pending = []
    sources_by_key = {}
    for grading_id, index, item, context in _pending_sources(db, user):
        question = item["question"].strip()
        key = (question, json.dumps(context, sort_keys=True))
        if key in sources_by_key:
            sources_by_key[key].append((grading_id, index, item, context))
            continue
        if _existing_pending_question(db, user, question, context):
            continue
        sources_by_key[key] = [(grading_id, index, item, context)]
        pending.append({
            "question": question, "question_preview": question,
            "student_answer": item.get("student_answer", ""),
            "model_answer": item["model_answer"], "ai_answer": item["model_answer"],
            "answer": item["model_answer"], "result": item.get("result", ""),
            "explanation": item.get("explanation", ""),
            "grading_id": grading_id, "detail_index": index, "teaching_context": context,
        })

    total = len(pending)
    start = (page - 1) * page_size
    items = pending[start:start + page_size]
    for entry in items:
        key = (entry["question"], json.dumps(entry["teaching_context"], sort_keys=True))
        knowledge = _grading_knowledge(db, sources_by_key[key])
        entry.update(
            chapter=knowledge["chapter"], topic=knowledge["topic"],
            classification_status=knowledge["status"],
            classification_source="grading" if knowledge["status"] == "classified" else "",
        )
    return {
        "pending": items,
        "items": items,
        "count": total,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/bank/confirm-answers")
def confirm_answers(
    req: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """老师确认后，将答案批量入库。"""
    imported = 0
    skipped = 0
    sources = list(_pending_sources(db, user))
    resolved = []
    # Resolve every source and its immutable scope before writing any answer.
    for ans in req.answers:
        matches = [source for source in sources if source[2]["question"].strip() == ans.question.strip()
                   and (ans.grading_id is None or source[0] == ans.grading_id)
                   and (ans.detail_index is None or source[1] == ans.detail_index)]
        if not matches:
            raise HTTPException(404, detail="待入库来源不存在、已删除或不属于当前老师")
        contexts = {json.dumps(source[3], sort_keys=True) for source in matches}
        if len(contexts) != 1:
            raise HTTPException(409, detail="同题存在多个学段来源，请刷新待入库页面后重新选择")
        context = matches[0][3]
        with use_teaching_context(context):
            try:
                _validate_import_scope([ans.model_dump()])
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc)) from exc
        resolved.append((ans, context, _bank_knowledge(ans, _grading_knowledge(db, matches))))

    for ans, context, (chapter, topic, chosen_by) in resolved:
        if not ans.question.strip() or not ans.answer.strip():
            skipped += 1
            continue

        # 去重检查
        existing = _existing_pending_question(db, user, ans.question.strip(), context)

        if existing:
            skipped += 1
            continue

        # Reuse the grading classifier's label instead of classifying again;
        # a teacher's own pick is confirmed; neither leaves it to the worker.
        row = QuestionBank(
            **record_teaching_fields(context, chapter=chapter, topic=topic),
            teacher_id=user.id, is_public=False, question=ans.question.strip(), answer=ans.answer.strip(),
            chapter=chapter, topic=topic, source="teacher_confirmed", grading_method="exact",
            classification_source="teacher" if chosen_by == "teacher" else "",
        )
        if chosen_by == "grading":
            mark_question_grading_classified(row)
        db.add(row)
        db.flush()
        imported += 1

    db.commit()
    return {"ok": True, "imported": imported, "skipped": skipped}


class PendingSource(BaseModel):
    grading_id: int = Field(..., ge=1)
    detail_index: int = Field(..., ge=0)


class DeletePendingRequest(BaseModel):
    questions: list[str] = Field(..., min_length=1, max_length=100)
    sources: list[PendingSource] = Field(default_factory=list, max_length=100)


@router.post("/bank/pending-answers/delete")
def delete_pending_answers(
    req: DeletePendingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """忽略指定的待入库题目：只记录忽略，不改写任何正式批改记录。

    忽略按"题干 + 教学范围"生效，同一道题在其他学生记录里的副本也一并隐藏，
    不会在下一次刷新时从另一名学生的记录里重新冒出来。
    """
    question_set = {q.strip() for q in req.questions if q.strip()}
    if not question_set:
        raise HTTPException(400, "questions 不能为空")

    source_ids = {(source.grading_id, source.detail_index) for source in req.sources}
    keys: dict[tuple[str, str], str] = {}
    for grading_id, index, item, context in _pending_sources(db, user):
        question = item["question"].strip()
        if question not in question_set:
            continue
        if source_ids and (grading_id, index) not in source_ids:
            continue
        keys.setdefault(_dismissal_key(question, context), question)

    for (question_hash, context_key), question in keys.items():
        db.add(PendingAnswerDismissal(
            teacher_id=user.id,
            question_hash=question_hash,
            context_key=context_key,
            question_preview=question[:200],
        ))
    db.commit()
    return {"ok": True, "removed": len(keys), "dismissed": len(keys)}
