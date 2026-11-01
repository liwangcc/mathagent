"""学生姓名确认API — 老师确认/修改OCR识别结果"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import Student, GradingResult
from models_user import User
from routers.auth import _require_user
from services.student_service import canonical_student_no

logger = logging.getLogger(__name__)
router = APIRouter()


class StudentConfirmRequest(BaseModel):
    upload_id: int = Field(..., ge=1)
    confirmed_name: str = Field(..., min_length=1, max_length=50)
    confirmed_student_no: str | None = Field(None, max_length=50)


@router.post("/student/confirm")
def confirm_student_info(
    data: StudentConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """老师确认/修改OCR识别的学生姓名和学号。

    Request body:
        {
            "upload_id": int,              # grading_result id (必填)
            "confirmed_name": str,         # 确认后的姓名 (必填)
            "confirmed_student_no": str,   # 确认后的学号 (可选)
        }

    Response:
        {
            "ok": true,
            "student_id": int,
            "student_name": str,
            "student_no": str,
        }
    """
    upload_id = data.upload_id
    confirmed_name = data.confirmed_name.strip()
    # 规范化学号：个位数补零（8→08），与批改落库口径一致，避免裸个位数入库。
    confirmed_student_no = canonical_student_no(data.confirmed_student_no) or ""

    if not upload_id:
        raise HTTPException(400, detail="upload_id 必填")
    if not confirmed_name:
        raise HTTPException(400, detail="confirmed_name 必填")

    # Find the grading result to locate the student (must belong to this teacher)
    result = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            GradingResult.id == upload_id,
            GradingResult.deleted_at.is_(None),
            Student.teacher_id == user.id,
        )
        .first()
    )
    if not result:
        raise HTTPException(404, detail="批改记录不存在")

    student = db.query(Student).filter(
        Student.id == result.student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生记录不存在")

    old_name = student.name
    old_no = student.student_no

    # 优先级 1：老师填的学号是最强身份信号。若已有学生（非当前占位生）持有该学号，
    # 把本次批改结果改挂到那个学生——这正是"同名靠学号区分"的落点，杜绝按姓名
    # .first() 归并到任意一个同名学生（那样等于没消歧）。
    if confirmed_student_no:
        by_no = db.query(Student).filter(
            Student.student_no == confirmed_student_no,
            Student.id != student.id,
            Student.teacher_id == user.id,
        ).first()
        if by_no:
            logger.info("Routing grading %d to student by 学号 %s: %s (id=%d)",
                        upload_id, confirmed_student_no, by_no.name, by_no.id)
            result.student_id = by_no.id
            db.commit()
            return {
                "ok": True,
                "student_id": by_no.id,
                "student_name": by_no.name,
                "student_no": by_no.student_no or "",
                "merged": True,
            }

    # 优先级 2：无学号时才按姓名归并（仅当姓名变更且目标唯一才安全）。
    # 有学号时绝不走这里——学号是权威，上面没命中就说明是"新号"，应写到当前学生身上，
    # 而不能按同名 .first() 归并到任意一个同名学生（那正是要消除的误配）。
    if not confirmed_student_no and confirmed_name != old_name:
        existing = db.query(Student).filter(
            Student.name == confirmed_name,
            Student.id != student.id,
            Student.teacher_id == user.id,
        ).first()
        if existing:
            # Merge: reassign grading result to existing student
            logger.info("Merging student %s (id=%d) into existing %s (id=%d)",
                        old_name, student.id, confirmed_name, existing.id)
            result.student_id = existing.id
            # Update existing student's number if provided and not set
            if confirmed_student_no and not existing.student_no:
                existing.student_no = confirmed_student_no
            db.commit()
            return {
                "ok": True,
                "student_id": existing.id,
                "student_name": existing.name,
                "student_no": existing.student_no or "",
                "merged": True,
            }

    # Update student info
    student.name = confirmed_name
    if confirmed_student_no:
        # Check student_no uniqueness
        if confirmed_student_no != old_no:
            dup = db.query(Student).filter(
                Student.student_no == confirmed_student_no,
                Student.id != student.id,
                Student.teacher_id == user.id,
            ).first()
            if dup:
                raise HTTPException(400, detail=f"学号 {confirmed_student_no} 已被学生 {dup.name} 使用")
        student.student_no = confirmed_student_no

    db.commit()
    db.refresh(student)

    logger.info("Student confirmed: id=%d, name: %s->%s, no: %s->%s",
                student.id, old_name, student.name,
                old_no or "(none)", student.student_no or "(none)")

    return {
        "ok": True,
        "student_id": student.id,
        "student_name": student.name,
        "student_no": student.student_no or "",
    }


@router.get("/student/pending-confirm")
def list_pending_confirm(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """列出需要确认姓名的批改结果。

    通过 grading_details 中的 needs_name_confirm 标记查找。
    """
    results = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == user.id,
            GradingResult.deleted_at.is_(None),
        )
        .order_by(GradingResult.id.desc())
        .limit(200)
        .all()
    )
    pending = []
    for r in results:
        details = r.get_details()
        # Check if any detail has needs_name_confirm
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict) and d.get("needs_name_confirm"):
                    student = db.query(Student).filter(
                        Student.id == r.student_id,
                        Student.teacher_id == user.id,
                    ).first()
                    pending.append({
                        "grading_result_id": r.id,
                        "student_id": r.student_id,
                        "student_name": student.name if student else "",
                        "student_no": student.student_no if student else "",
                        "ocr_name": d.get("ocr_name", ""),
                        "ocr_student_no": d.get("ocr_student_no", ""),
                        "confidence": d.get("student_info_confidence", 0),
                        "exam_name": r.exam_name,
                        "score": r.score,
                    })
                    break
        if len(pending) >= limit:
            break
    return pending
