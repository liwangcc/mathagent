"""老师待处理面板接口 — 任务6"""

import copy
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import GradingCorrection, KnowledgeEvent, QuestionBank, GradingResult, Student
from models_user import User
from routers.auth import _require_user
from services.bank_scope import visible_filter, owned_filter
from services.scoring_engine import recalculate_result_from_details
from services.math_curriculum import catalog_version, get_knowledge_point, get_math_catalog
from services.practice import filter_bank_stage
from services.teaching_context import require_teacher_stage
from config import FAILED_DIR

logger = logging.getLogger(__name__)
router = APIRouter()
RESULT_LIMIT = 200


def _filter_bank_view(query, user, education_stage=None, *, profile_only=False):
    default_stage = require_teacher_stage(user)
    if profile_only and education_stage not in (None, "", default_stage):
        raise HTTPException(409, detail={
            "code": "teaching_profile_changed",
            "message": "题库学段已跟随个人资料更新，请刷新后重试",
        })
    stage = education_stage or default_stage
    if stage == "legacy":
        return query.filter((QuestionBank.education_stage == "") | QuestionBank.education_stage.is_(None))
    return filter_bank_stage(query, stage)


def _same_record_stage(query, education_stage):
    """A historical confirmation cannot overwrite a same-text new-stage row."""
    if education_stage:
        return query.filter(QuestionBank.education_stage == education_stage)
    return query.filter((QuestionBank.education_stage == "") | QuestionBank.education_stage.is_(None))


def _validate_bank_knowledge(stage, chapter, topic):
    if not stage or not (chapter or topic):
        return None
    modules = get_math_catalog(stage)
    if not any(module["chapter"] == chapter for module in modules):
        raise HTTPException(422, detail="请选择当前题目学段知识目录中的模块")
    if not topic:
        return None
    point = get_knowledge_point(stage, chapter, topic)
    if point is None:
        raise HTTPException(422, detail="知识点不属于当前题目学段的所选模块")
    return point


def _assert_can_write_question(q, user):
    """题库写操作归属校验（P0-4）：普通老师只能改/删自己私库的题；
    公库题（teacher_id IS NULL）及他人私库题仅 admin 可写。"""
    is_admin = (user.role or "").lower() == "admin"
    if is_admin:
        return
    if q.teacher_id != user.id:
        raise HTTPException(403, detail="无权操作该题目（公库题或他人题目）")


def _mark_question_matcher_dirty() -> None:
    """Force future matching to rebuild after a committed bank mutation."""
    from services.grader import GraderService
    GraderService._index_dirty = True


def _delete_question_preserving_history(
    db: Session,
    question: QuestionBank,
    *,
    commit: bool = True,
) -> int:
    """Delete a bank question without erasing historical student learning events.

    A teacher removing a private source question is curating future matching, not
    revoking past grading. Historical events retain their topic/result data but
    no longer point to the deleted QuestionBank row. Callers that already own a
    larger transaction can pass ``commit=False`` and invalidate the matcher only
    after their own commit succeeds.
    """
    detached_events = (
        db.query(KnowledgeEvent)
        .filter(KnowledgeEvent.question_bank_id == question.id)
        .update({KnowledgeEvent.question_bank_id: None}, synchronize_session=False)
    )
    db.delete(question)
    if commit:
        db.commit()
        _mark_question_matcher_dirty()
    return detached_events


class ConfirmJudgmentRequest(BaseModel):
    action: str = Field("", pattern="^(correct|partial|wrong|void)?$")
    bank_action: str = Field("", pattern="^(add|skip)?$")
    detail_index: int = Field(0, ge=0)


class ImagePathRequest(BaseModel):
    image_path: str = Field(..., min_length=1, max_length=500)


class BankQuestionCreateRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000)
    answer: str = Field("", max_length=5000)
    subject: Literal["数学"] = "数学"
    chapter: str = Field("", max_length=100)
    topic: str = Field("", max_length=100)
    grading_method: str = Field("exact", max_length=50)


class BankQuestionUpdateRequest(BaseModel):
    question: str | None = Field(None, min_length=1, max_length=5000)
    answer: str | None = Field(None, max_length=5000)
    chapter: str | None = Field(None, max_length=100)
    topic: str | None = Field(None, max_length=100)
    grading_method: str | None = Field(None, max_length=50)


def _to_url(file_path: str) -> str:
    """Convert an absolute file path to an application URL."""
    if not file_path:
        return ""
    norm = file_path.replace("\\", "/")
    for marker in ("uploads/", "批阅失败/"):
        idx = norm.find(marker)
        if idx != -1:
            return "/" + norm[idx:]
    return "/" + norm


def _teacher_failed_dir(teacher_id: int) -> str:
    """Return the only failed-import directory visible to one teacher."""
    return os.path.join(FAILED_DIR, str(int(teacher_id)))


def _failed_path_from_url(image_path: str, teacher_id: int) -> str:
    """Resolve a client URL inside the current teacher's failed directory only."""
    prefix = "/批阅失败/"
    if image_path.startswith(prefix):
        relative = image_path[len(prefix):]
        candidate = os.path.join(FAILED_DIR, *relative.split("/"))
    else:
        candidate = image_path[1:] if re.match(r"^/[A-Za-z]:", image_path) else image_path
    real = os.path.realpath(candidate)
    owned_root = os.path.realpath(_teacher_failed_dir(teacher_id))
    if real == owned_root or not real.startswith(owned_root + os.sep):
        raise HTTPException(404, detail="失败导入不存在")
    return real


@router.get("/teacher/pending")
def get_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Get pending items: only LLM failures (recognition + reasoning errors)."""
    llm_failures = []
    results = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == user.id,
            GradingResult.deleted_at.is_(None),
        )
        .order_by(GradingResult.id.desc())
        .limit(RESULT_LIMIT)
        .all()
    )
    student_ids = {r.student_id for r in results}
    students_map = {
        s.id: s
        for s in db.query(Student).filter(
            Student.id.in_(student_ids),
            Student.teacher_id == user.id,
        ).all()
    } if student_ids else {}
    for r in results:
        details = r.get_details()
        student = students_map.get(r.student_id)
        for d in details:
            # 显示LLM真正失败的（异常）和LLM无法判断的（needs_review）
            if d.get("result") == "needs_review":
                llm_failures.append({
                    "id": r.id,
                    "detail_index": d.get("index"),
                    "student_name": student.name if student else "未知",
                    "student_no": student.student_no if student else "",
                    "question_text": d.get("question", ""),
                    "student_answer": d.get("student_answer", ""),
                    "status": d.get("result"),
                    "llm_explanation": d.get("explanation", ""),
                    "image_path": _to_url(d.get("image_path", "")),
                })

    total = len(llm_failures)
    start = (page - 1) * page_size
    items = llm_failures[start:start + page_size]
    return {
        "llm_failures": items,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/teacher/confirm/{grading_result_id}")
def confirm_judgment(
    grading_result_id: int,
    data: ConfirmJudgmentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Teacher's two independent decisions per question (parallel).

    action: "correct"|"partial"|"wrong"|"void"  — judging the student
    bank_action: "add"|"skip"  — whether to add model_answer to question bank
    Both must be set for the item to disappear from pending.
    "void" action clears the item immediately without needing bank_action.
    """
    action = data.action
    bank_action = data.bank_action
    detail_index = data.detail_index

    result = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            GradingResult.id == grading_result_id,
            GradingResult.deleted_at.is_(None),
            Student.teacher_id == user.id,
        )
        .first()
    )
    if not result:
        raise HTTPException(404, detail="批改记录不存在")

    details = result.get_details()
    if detail_index >= len(details):
        raise HTTPException(400, detail="题目索引无效")
    previous_details = copy.deepcopy(details)

    auto_question_deleted = False
    d = details[detail_index]

    # Left side: judgment
    if action in ("correct", "partial", "wrong"):
        d["result"] = action
        d["confirmed"] = True
        d["teacher_modified"] = True
    elif action == "void":
        d["result"] = "void"
        d["confirmed"] = True
        # Remove any auto-imported question for this voided item
        question_text = d.get("question", "")
        if question_text:
            auto_q = _same_record_stage(db.query(QuestionBank), result.education_stage).filter(
                QuestionBank.question == question_text,
                QuestionBank.source == "auto",
                owned_filter(user.id),
            ).first()
            if auto_q:
                _delete_question_preserving_history(db, auto_q, commit=False)
                auto_question_deleted = True

    # Right side: bank decision
    if bank_action == "add":
        model_answer = d.get("model_answer", "")
        question_text = d.get("question", "")
        if question_text and model_answer:
            existing = _same_record_stage(db.query(QuestionBank), result.education_stage).filter(
                QuestionBank.question == question_text,
                owned_filter(user.id),
            ).first()
            if existing:
                existing.answer = model_answer
                existing.source = "teacher_confirmed"
            else:
                from services.correction_tracking import grading_result_teaching_context
                from services.question_classification import (
                    grading_knowledge_for_detail,
                    mark_question_grading_classified,
                )

                # Reuse the grading classifier's label rather than queueing
                # the same text for a second classification request.
                try:
                    knowledge = grading_knowledge_for_detail(
                        db, grading_result_id=result.id, detail_index=detail_index,
                        detail=d, context=grading_result_teaching_context(result),
                    )
                except ValueError:
                    knowledge = {"status": "failed", "chapter": "", "topic": ""}
                q = QuestionBank(
                    teacher_id=user.id,
                    is_public=False,
                    subject="数学",
                    question=question_text,
                    answer=model_answer,
                    chapter=knowledge["chapter"],
                    topic=knowledge["topic"],
                    grading_method="exact",
                    source="teacher_confirmed",
                    usage_count=0,
                    education_stage=result.education_stage or "",
                    catalog_version=result.catalog_version or "",
                )
                if knowledge["status"] == "classified":
                    point = (
                        get_knowledge_point(q.education_stage, q.chapter, q.topic)
                        if q.education_stage else None
                    )
                    q.knowledge_point_id = point["id"] if point else ""
                    mark_question_grading_classified(q)
                db.add(q)

    # Recalculate grades (exclude voided questions)
    valid = [x for x in details if x.get("result") != "void"]
    aggregate = {"details": valid}
    recalculate_result_from_details(aggregate, result.category or "daily")
    result.total_questions = aggregate["total"]
    result.correct_count = aggregate["correct_count"]
    result.partial_count = aggregate["partial_count"]
    result.wrong_count = aggregate["wrong_count"]
    result.needs_review_count = aggregate["needs_review_count"]
    result.score = aggregate["score"]
    result.max_score = aggregate["max_score"]
    result.correct_rate = aggregate["correct_rate"]
    result.scoring_version = int(aggregate.get("scoring_version") or 2)

    result.set_details(details)
    if action and previous_details[detail_index].get("result") != d.get("result"):
        changed_at = datetime.now(timezone.utc).isoformat()
        audit_key = hashlib.sha256(
            f"pending-confirm:{result.id}:{detail_index}:{changed_at}:{action}".encode("utf-8")
        ).hexdigest()
        db.add(GradingCorrection(
            grading_result_id=result.id,
            teacher_id=user.id,
            paper_id="pending-judgment",
            question_no=str(
                d.get("parent_question_no")
                or d.get("question_no")
                or d.get("question_number")
                or detail_index + 1
            ),
            audit_key=audit_key,
            old_result=str(previous_details[detail_index].get("result") or ""),
            new_result=str(d.get("result") or ""),
            note="教师在待处理面板确认",
        ))
        from services.grading_persistence import synchronize_teacher_confirmation

        synchronize_teacher_confirmation(
            db,
            student=db.get(Student, result.student_id),
            teacher_id=user.id,
            grading_result=result,
            previous_details=previous_details,
            details=details,
            detail_index=detail_index,
        )
    db.commit()
    if auto_question_deleted:
        _mark_question_matcher_dirty()
    return {"ok": True}


@router.get("/teacher/failed")
def get_failed_imports(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user: User = Depends(_require_user),
):
    """List failed imports owned by the current teacher."""
    failed = []
    teacher_dir = _teacher_failed_dir(user.id)
    if os.path.exists(teacher_dir):
        for fname in sorted(os.listdir(teacher_dir), reverse=True):
            path = os.path.join(teacher_dir, fname)
            if fname.endswith(".txt"):
                continue
            note_path = path + ".txt"
            reason = "未知原因"
            if os.path.exists(note_path):
                with open(note_path, "r", encoding="utf-8") as f:
                    reason = f.read().strip()
            failed.append({
                "image_path": _to_url(path),
                "filename": fname,
                "reason": reason,
            })
    total = len(failed)
    start = (page - 1) * page_size
    items = failed[start:start + page_size]
    return {
        "failed_imports": items,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/teacher/failed")
def delete_failed_import(data: ImagePathRequest, user: User = Depends(_require_user)):
    """Delete one failed import owned by the current teacher."""
    image_path = data.image_path
    if not image_path:
        raise HTTPException(400, detail="需要 image_path")

    real_abs = _failed_path_from_url(image_path, user.id)
    deleted = []
    if os.path.exists(real_abs):
        os.remove(real_abs)
        deleted.append(real_abs)
    note_path = real_abs + ".txt"
    if os.path.exists(note_path):
        os.remove(note_path)
        deleted.append(note_path)
    return {"ok": True, "deleted": deleted}


@router.delete("/teacher/failed/all")
def delete_all_failed_imports(user: User = Depends(_require_user)):
    """Delete all failed import records owned by the current teacher."""
    deleted = 0
    teacher_dir = _teacher_failed_dir(user.id)
    if os.path.exists(teacher_dir):
        for fname in os.listdir(teacher_dir):
            fpath = os.path.join(teacher_dir, fname)
            try:
                os.remove(fpath)
                deleted += 1
            except OSError as exc:
                logger.warning("删除失败导入文件失败 teacher=%s path=%s: %s", user.id, fpath, exc)
    return {"ok": True, "deleted": deleted}


@router.get("/bank/auto")
def list_auto_answers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy"] | None = None,
):
    """List auto-inserted question bank answers."""
    query = _filter_bank_view(db.query(QuestionBank), user, education_stage).filter(
        QuestionBank.source == "auto", visible_filter(user.id)
    )
    total = query.count()
    qs = query.order_by(QuestionBank.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    items = [q.to_dict() for q in qs]
    return {"items": items, "questions": items, "total": total, "page": page, "page_size": page_size}

@router.get("/bank/export")
def export_private_bank(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy"] | None = None,
):
    """Export the current user private question bank as JSON."""
    qs = _filter_bank_view(db.query(QuestionBank), user, education_stage, profile_only=True).filter(
        QuestionBank.teacher_id == user.id,
        QuestionBank.is_public == False,
    ).order_by(QuestionBank.id.asc()).all()

    items = []
    for q in qs:
        items.append({
            "question": q.question,
            "answer": q.answer,
            "chapter": q.chapter or "",
            "topic": q.topic or "",
            "difficulty": q.difficulty or 1,
            "education_stage": q.education_stage or "",
            "catalog_version": q.catalog_version or "",
            "knowledge_point_id": q.knowledge_point_id or "",
        })

    filename = f"mathagent_bank_{user.id}_{datetime.now().strftime('%Y%m%d')}.json"
    return JSONResponse(
        content={"items": items, "count": len(items), "exported_by": user.name or str(user.id)},
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )




# --- Question Bank CRUD ---

@router.get("/teacher/bank")
def list_bank(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    chapter: str = None,
    keyword: str = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy"] | None = None,
):
    """List all question bank entries (paginated)."""
    offset = (page - 1) * page_size
    query = _filter_bank_view(db.query(QuestionBank), user, education_stage, profile_only=True).filter(visible_filter(user.id))
    if chapter:
        from services.security import escape_like
        query = query.filter(QuestionBank.chapter.like(f"%{escape_like(chapter)}%", escape="\\"))
    if keyword:
        from services.security import escape_like
        kw = f"%{escape_like(keyword)}%"
        query = query.filter(
            (QuestionBank.question.like(kw, escape="\\"))
            | (QuestionBank.answer.like(kw, escape="\\"))
        )
    total = query.count()
    qs = query.order_by(QuestionBank.id.desc()).offset(offset).limit(page_size).all()
    return {"items": [q.to_dict() for q in qs], "total": total, "page": page, "page_size": page_size}


@router.post("/teacher/bank")
def add_to_bank(
    data: BankQuestionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Add a new question to the bank."""
    stage = require_teacher_stage(user)
    payload = data.model_dump()
    question = payload["question"].strip()
    if not question:
        raise HTTPException(400, detail="题目内容不能为空")
    if _same_record_stage(db.query(QuestionBank), stage).filter(
        owned_filter(user.id), QuestionBank.question == question,
    ).first():
        raise HTTPException(409, detail="当前学段私库已存在相同题目")
    point = _validate_bank_knowledge(stage, payload.get("chapter", ""), payload.get("topic", ""))
    q = QuestionBank(
        teacher_id=user.id,
        is_public=False,
        subject=payload.get("subject", "数学"),
        question=question,
        answer=payload.get("answer", ""),
        chapter=payload.get("chapter", ""),
        topic=payload.get("topic", ""),
        grading_method=payload.get("grading_method", "exact"),
        source="manual",
        usage_count=0,
        education_stage=stage,
        catalog_version=catalog_version(stage),
        knowledge_point_id=point["id"] if point else "",
    )
    db.add(q)
    db.commit()
    return {"ok": True, "id": q.id}


class LegacyBankStageRequest(BaseModel):
    question_ids: list[int] = Field(..., min_length=1, max_length=200)
    education_stage: Literal["primary", "junior", "senior"]


@router.post("/teacher/bank/legacy-stage")
def assign_legacy_bank_stage(data: LegacyBankStageRequest, db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """Explicit teacher adoption only; never silently relabel a staged record."""
    require_teacher_stage(user)
    ids = set(data.question_ids)
    if any(question_id < 1 for question_id in ids):
        raise HTTPException(422, detail="题目 ID 无效")
    rows = db.query(QuestionBank).filter(QuestionBank.id.in_(ids)).all()
    if len(rows) != len(ids):
        raise HTTPException(404, detail="部分题目不存在，请刷新后重试")
    is_admin = (user.role or "").lower() == "admin"
    for row in rows:
        if not ((row.teacher_id == user.id and not row.is_public) or (is_admin and row.teacher_id is None and row.is_public)):
            raise HTTPException(403, detail="只能迁入自己的私库；公共题仅管理员可设置学段")
        if row.education_stage:
            raise HTTPException(409, detail="只能迁入历史未标学段题目，不能修改已有学段")
        if (row.subject or "数学").strip() != "数学":
            raise HTTPException(422, detail="仅支持将数学题迁入数学目录")
    stage = data.education_stage
    chapters = {entry["chapter"] for entry in get_math_catalog(stage)}
    from services.question_classification import mark_question_classified, queue_question_classification
    pending = 0
    for row in rows:
        row.education_stage = stage
        row.catalog_version = catalog_version(stage)
        point = get_knowledge_point(stage, row.chapter or "", row.topic or "")
        if point:
            row.knowledge_point_id = point["id"]
            mark_question_classified(db, row, source="teacher_stage")
        else:
            if (row.chapter or "") not in chapters:
                row.chapter = ""
            row.topic = ""
            queue_question_classification(db, row)
            pending += 1
    db.commit()
    _mark_question_matcher_dirty()
    return {"ok": True, "updated": len(rows), "classification_pending": pending, "education_stage": stage}


@router.put("/teacher/bank/{question_id}")
def update_bank(
    question_id: int,
    data: BankQuestionUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Update an existing question in the bank."""
    q = db.query(QuestionBank).filter(QuestionBank.id == question_id).first()
    if not q:
        raise HTTPException(404, detail="题目不存在")
    _assert_can_write_question(q, user)
    payload = data.model_dump(exclude_unset=True)
    point = _validate_bank_knowledge(
        q.education_stage,
        payload.get("chapter", q.chapter) or "",
        payload.get("topic", q.topic) or "",
    ) if "chapter" in payload or "topic" in payload else None
    if "question" in payload:
        q.question = payload["question"].strip()
    if "answer" in payload:
        q.answer = payload["answer"] or ""
    if "chapter" in payload:
        q.chapter = payload["chapter"] or ""
    if "topic" in payload:
        q.topic = payload["topic"] or ""
    if "grading_method" in payload:
        q.grading_method = payload["grading_method"] or "exact"
    if q.education_stage and ("chapter" in payload or "topic" in payload):
        q.knowledge_point_id = point["id"] if point else ""
        q.catalog_version = catalog_version(q.education_stage)
    if str(q.chapter or "").strip() and str(q.topic or "").strip():
        from services.question_classification import mark_question_classified

        mark_question_classified(db, q, source="teacher")
    else:
        from services.question_classification import queue_question_classification

        queue_question_classification(db, q)
    db.commit()
    return {"ok": True}


@router.delete("/teacher/bank/{question_id}")
def delete_from_bank(
    question_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Delete a question from the bank."""
    q = db.query(QuestionBank).filter(QuestionBank.id == question_id).first()
    if not q:
        raise HTTPException(404, detail="题目不存在")
    _assert_can_write_question(q, user)
    _delete_question_preserving_history(db, q)
    return {"ok": True}


@router.get("/teacher/bank/unconfirmed")
def list_unconfirmed_bank(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy"] | None = None,
):
    """List auto-added questions waiting for teacher confirmation."""
    query = _filter_bank_view(db.query(QuestionBank), user, education_stage).filter(
        QuestionBank.source == "auto", visible_filter(user.id)
    )
    total = query.count()
    qs = query.order_by(QuestionBank.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    items = [q.to_dict() for q in qs]
    return {"items": items, "questions": items, "total": total, "page": page, "page_size": page_size}


@router.post("/teacher/bank/{question_id}/confirm")
def confirm_bank_question(
    question_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Teacher confirms an auto-added question — promote to teacher_confirmed."""
    q = db.query(QuestionBank).filter(QuestionBank.id == question_id).first()
    if not q:
        raise HTTPException(404, detail="题目不存在")
    _assert_can_write_question(q, user)
    if q.source != "auto":
        return {"ok": False, "error": "该题不是自动入库状态，无需确认"}
    q.source = "teacher_confirmed"
    db.commit()
    from services.grader import GraderService
    GraderService._index_dirty = True
    return {"ok": True}


@router.post("/teacher/bank/{question_id}/reject")
def reject_bank_question(
    question_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Teacher rejects an auto-added question — delete from bank."""
    q = db.query(QuestionBank).filter(QuestionBank.id == question_id).first()
    if not q:
        raise HTTPException(404, detail="题目不存在")
    _assert_can_write_question(q, user)
    if q.source != "auto":
        return {"ok": False, "error": "该题不是自动入库状态"}
    _delete_question_preserving_history(db, q)
    return {"ok": True}


@router.get("/teacher/chapters")
def get_chapters(user: User = Depends(_require_user), education_stage: str | None = None):
    """Get the selected mathematics-stage catalog from the single versioned source."""
    profile_stage = require_teacher_stage(user)
    stage = education_stage or profile_stage
    if stage == "legacy":
        return []
    if stage not in {"primary", "junior", "senior"}:
        raise HTTPException(422, detail="学段须为 primary、junior、senior 或 legacy")
    return get_math_catalog(stage)
