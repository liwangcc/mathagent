"""批改记录接口 — 历史批改结果的列表、详情与删除，以及学生档案列表。"""

import json as _json
import os
import logging
from datetime import datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from services.grading_contract import VALID_GRADING_CATEGORIES
from models import GradingResult, StudentProfile, Student
from models_user import User
from routers.auth import _require_user
from config import UPLOAD_DIR

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/grade/results")
def list_grading_results(
    student_id: Optional[int] = Query(None),
    page: Annotated[Optional[int], Query(ge=1)] = None,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    category: Annotated[Optional[Literal["daily", "exam"]], Query()] = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """查询批改结果列表（仅当前老师的学生）。

    传 page 时返回 {items,total,page,page_size} 分页结构；不传时保持旧的
    "最近 50 条数组"响应，兼容未升级的客户端。
    """
    # 获取当前老师所有学生ID
    student_ids = [s.id for s in db.query(Student.id).filter(Student.teacher_id == user.id).all()]
    if not student_ids:
        return [] if page is None else {"items": [], "total": 0, "page": page, "page_size": page_size}
    q = db.query(GradingResult).filter(
        GradingResult.student_id.in_(student_ids),
        GradingResult.deleted_at.is_(None),
    )
    if student_id:
        if student_id not in student_ids:
            raise HTTPException(404, detail="学生不存在")
        q = q.filter(GradingResult.student_id == student_id)
    if category:
        q = q.filter(GradingResult.category == category)
    if page is None:
        results = q.order_by(GradingResult.id.desc()).limit(50).all()
        return [r.to_dict() for r in results]
    total = q.count()
    results = q.order_by(GradingResult.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [r.to_dict() for r in results], "total": total, "page": page, "page_size": page_size}


@router.delete("/grade/results")
def delete_grading_results(
    ids: list[int],
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """批量删除批改结果（仅当前老师的学生）"""
    if not ids:
        raise HTTPException(400, detail="请选择要删除的记录")

    # 获取当前老师所有学生ID
    student_ids = [s.id for s in db.query(Student.id).filter(Student.teacher_id == user.id).all()]
    if not student_ids:
        raise HTTPException(404, detail="无学生数据")

    # A teacher hiding a record keeps its derived archive and analytics history.
    deleted = db.query(GradingResult).filter(
        GradingResult.id.in_(ids),
        GradingResult.student_id.in_(student_ids),
        GradingResult.deleted_at.is_(None),
    ).update({GradingResult.deleted_at: datetime.now()}, synchronize_session=False)
    db.commit()

    return {"deleted": deleted}


@router.get("/grade/result/{result_id}")
def get_grading_result(
    result_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """获取单次批改详情"""
    result = db.query(GradingResult).filter(
        GradingResult.id == result_id,
        GradingResult.deleted_at.is_(None),
    ).first()
    if not result:
        raise HTTPException(404, detail="批改结果不存在")
    # 校验所属学生属于当前老师
    student = db.query(Student).filter(
        Student.id == result.student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="批改结果不存在")
    payload = result.to_dict()
    source_assets = payload.get("source_assets")
    if isinstance(source_assets, dict) and source_assets.get("source") == "answer_sheet":
        source_assets["available"] = False
        try:
            from services.answer_sheet_session import AnswerSheetSession

            session_id = str(source_assets.get("session_id") or "")
            session = AnswerSheetSession.load(session_id, UPLOAD_DIR)
            lifecycle = session.asset_lifecycle if isinstance(session.asset_lifecycle, dict) else {}
            expired = lifecycle.get("status") == "expired"
            page_by_id = {
                str(page.get("paper_id") or ""): page
                for page in session.student_pages
                if isinstance(page, dict)
            }
            session_root = os.path.realpath(session.session_dir)
            pages_available = bool(source_assets.get("pages"))
            for page_ref in source_assets.get("pages") or []:
                page = page_by_id.get(str(page_ref.get("paper_id") or "")) or {}
                image_path = os.path.realpath(str(page.get("path") or ""))
                if (
                    not image_path.startswith(session_root + os.sep)
                    or not os.path.isfile(image_path)
                ):
                    pages_available = False
                    break
            source_assets["available"] = bool(
                session.teacher_id == user.id and not expired and pages_available
            )
            if expired:
                source_assets["expired_at"] = str(lifecycle.get("expired_at") or "")
        except (OSError, ValueError, TypeError, KeyError, _json.JSONDecodeError):
            source_assets["available"] = False
    return payload


@router.delete("/grading/{result_id}")
def delete_grading_result(
    result_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Hide one grading record while preserving its archive and analytics history."""
    result = db.query(GradingResult).filter(
        GradingResult.id == result_id,
        GradingResult.deleted_at.is_(None),
    ).first()
    if not result:
        raise HTTPException(404, detail="批改记录不存在")
    # 校验所属学生属于当前老师
    student = db.query(Student).filter(
        Student.id == result.student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="批改记录不存在")

    result.deleted_at = datetime.now()
    db.commit()
    return {"ok": True, "deleted_files": 0}


@router.get("/profiles/list")
def list_profiles(
    student_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """查询学生档案（仅当前老师的学生）"""
    if category:
        category = str(category).strip().lower()
        if category not in VALID_GRADING_CATEGORIES:
            raise HTTPException(400, detail="category 必须是 daily 或 exam")
    # 获取当前老师所有学生ID
    student_ids = [s.id for s in db.query(Student.id).filter(Student.teacher_id == user.id).all()]
    if not student_ids:
        return []
    q = db.query(StudentProfile).filter(
        StudentProfile.student_id.in_(student_ids),
        StudentProfile.analytics_included.is_(True),
    )
    if student_id:
        if student_id not in student_ids:
            raise HTTPException(404, detail="学生不存在")
        q = q.filter(StudentProfile.student_id == student_id)
    if category:
        q = q.filter(StudentProfile.category == category)
    profiles = q.order_by(StudentProfile.created_at.desc()).limit(50).all()
    return [p.to_dict() for p in profiles]
