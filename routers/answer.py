"""答案管理接口"""

import asyncio
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session

from database import get_db
from models import QuestionBank
from models_user import User
from routers.auth import _require_user
from services.answer_import import import_from_json, import_from_excel, get_unclassified_questions
from services.bank_scope import visible_filter, owned_filter
from services.security import read_upload_capped
from services.teaching_context import await_teaching, make_teaching_context, require_teacher_stage, use_teaching_context
from config import MAX_IMPORT_FILE_SIZE

router = APIRouter()


@router.post("/answer/import/excel")
async def import_answer_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """从Excel导入答案"""
    context = make_teaching_context(require_teacher_stage(user))
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="请上传Excel文件(.xlsx/.xls)")

    content = await read_upload_capped(file, MAX_IMPORT_FILE_SIZE)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await await_teaching(context, asyncio.to_thread(
            import_from_excel,
            db,
            tmp_path,
            teacher_id=user.id,
        ))
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, detail=str(exc)) from exc
    finally:
        os.unlink(tmp_path)


@router.post("/answer/import/json")
async def import_answer_json(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """从JSON导入答案"""
    context = make_teaching_context(require_teacher_stage(user))
    if not file.filename.endswith(".json"):
        raise HTTPException(400, detail="请上传JSON文件(.json)")

    content = await read_upload_capped(file, MAX_IMPORT_FILE_SIZE)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await await_teaching(context, asyncio.to_thread(
            import_from_json,
            db,
            tmp_path,
            teacher_id=user.id,
        ))
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, detail=str(exc)) from exc
    finally:
        os.unlink(tmp_path)


@router.get("/answer/list")
def list_answers(
    chapter: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
    difficulty: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """查看题库答案列表"""
    with use_teaching_context(make_teaching_context(require_teacher_stage(user))):
        q = db.query(QuestionBank).filter(visible_filter(user.id))
    if chapter:
        q = q.filter(QuestionBank.chapter == chapter)
    if topic:
        q = q.filter(QuestionBank.topic == topic)
    if difficulty is not None:
        q = q.filter(QuestionBank.difficulty == difficulty)

    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [i.to_dict() for i in items]}


@router.get("/answer/unclassified")
def list_unclassified(db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """获取未分类的题目"""
    with use_teaching_context(make_teaching_context(require_teacher_stage(user))):
        return get_unclassified_questions(db, teacher_id=user.id)


@router.get("/answer/classification-status")
def classification_status(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """获取题库后台知识点分类进度。"""
    from services.question_classification import get_classification_status

    with use_teaching_context(make_teaching_context(require_teacher_stage(user))):
        return get_classification_status(db, teacher_id=user.id)


@router.put("/answer/{answer_id}/classify")
def classify_answer(
    answer_id: int,
    chapter: str,
    topic: str,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """手动分类题目"""
    # 写操作：普通老师只能改自己私库的题；公库题仅 admin 可改
    is_admin = (user.role or "").lower() == "admin"
    query = db.query(QuestionBank).filter(QuestionBank.id == answer_id)
    if not is_admin:
        query = query.filter(owned_filter(user.id))
    q = query.first()
    if not q:
        raise HTTPException(404, detail="题目不存在")
    chapter = (chapter or "").strip()
    topic = (topic or "").strip()
    if not chapter or not topic:
        raise HTTPException(400, detail="章节和知识点不能为空")
    from services.math_curriculum import get_knowledge_point
    if q.education_stage and get_knowledge_point(q.education_stage, chapter, topic) is None:
        raise HTTPException(422, detail="知识点不属于该题目原有学段目录")
    q.chapter = chapter
    q.topic = topic
    from services.question_classification import mark_question_classified

    mark_question_classified(db, q, source="teacher")
    db.commit()
    return q.to_dict()
