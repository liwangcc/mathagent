"""题库查询与出题接口"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import QuestionBank
from models_user import User
from routers.auth import _require_user
from services.bank_scope import visible_filter
from services.practice import load_knowledge_points, get_bank_statistics, filter_bank_stage
from services.teaching_context import make_teaching_context, require_teacher_stage, use_teaching_context
from services.security import escape_like
from services.paper_generator import (
    generate_practice_paper,
    export_paper_to_word,
)
from services.generated_files import register_and_respond

router = APIRouter()


@router.post("/bank/generate/practice")
def generate_practice(
    topic: str,
    count: int = 5,
    chapter: Optional[str] = None,
    subject: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """生成随堂练习卷"""
    context = make_teaching_context(require_teacher_stage(user))
    with use_teaching_context(context):
        paper = generate_practice_paper(db, topic=topic, count=count, chapter=chapter, subject=subject, teacher_id=user.id)
    paper["teaching_context"] = context
    return paper


@router.get("/bank/generate/export-practice")
def export_practice_docx(
    topic: str,
    count: int = 5,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """导出随堂练习为Word"""
    context = make_teaching_context(require_teacher_stage(user))
    with use_teaching_context(context):
        paper = generate_practice_paper(db, topic=topic, count=count, teacher_id=user.id)
    paper["teaching_context"] = context
    buf = export_paper_to_word(paper)
    return register_and_respond(db, user.id, buf, "随堂练习", topic, "docx")


@router.get("/bank/statistics")
def bank_statistics(db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """题库统计"""
    return get_bank_statistics(db, teacher_id=user.id, education_stage=require_teacher_stage(user))


@router.get("/bank/knowledge-points")
def knowledge_points_list(user: User = Depends(_require_user)):
    """获取知识点层级结构"""
    return load_knowledge_points(require_teacher_stage(user))


@router.get("/bank/pick")
def pick_from_bank(
    chapters: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """按章节从题库抽题"""
    from sqlalchemy import or_
    q = filter_bank_stage(db.query(QuestionBank).filter(visible_filter(user.id)), require_teacher_stage(user))
    if chapters:
        filters = [QuestionBank.chapter.like(f"%{escape_like(ch)}%", escape='\\') for ch in chapters]
        q = q.filter(or_(*filters))
    questions = q.order_by(func.random()).limit(50).all()
    return {"questions": [qu.to_dict() for qu in questions]}
