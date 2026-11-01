"""成绩导出与错题条API"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from services.exam_export import export_exam_scores, list_exams, list_classes
from services.generated_files import register_and_respond

logger = logging.getLogger(__name__)
router = APIRouter()
ExportStage = Literal["primary", "junior", "senior", "legacy"]


def _export_error(result):
    status = 409 if result.get("code") == "ambiguous_export_stage" else 404
    raise HTTPException(status, detail=result if status == 409 else result["error"])


class ExamNameRequest(BaseModel):
    exam_name: str = Field(..., min_length=1, max_length=100)
    class_name: str | None = Field(None, max_length=50)
    education_stage: ExportStage | None = None


# ── 成绩导出Excel ──

@router.get("/exam-export/exams")
def api_list_exams(
    class_name: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: ExportStage | None = None,
):
    """列出所有有成绩记录的考试"""
    exams = list_exams(db, user.id, class_name, education_stage=education_stage)
    classes = list_classes(db, user.id, education_stage=education_stage)
    return {"exams": exams, "classes": classes}


@router.get("/exam-export/classes")
def api_list_classes(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: ExportStage | None = None,
):
    """列出所有有成绩记录的班级"""
    return {"classes": list_classes(db, user.id, education_stage=education_stage)}


@router.post("/exam-export/scores")
def api_export_scores(
    data: ExamNameRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """导出指定考试的成绩Excel

    请求体:
        exam_name: str (必填) — 考试名称
        class_name: str (可选) — 班级名称，不填则导出全部班级
    """
    exam_name = data.exam_name
    class_name = data.class_name

    if not exam_name:
        raise HTTPException(400, detail="exam_name为必填参数")

    result = export_exam_scores(db, user.id, exam_name, class_name, education_stage=data.education_stage)
    if isinstance(result, dict) and "error" in result:
        _export_error(result)

    # 文件名: {班级}_{考试名}_{日期}.xlsx
    cls = class_name or "全部班级"
    return register_and_respond(db, user.id, result, "成绩表", f"{cls}_{exam_name}", "xlsx")


@router.get("/exam-export/scores/{exam_name}")
def api_export_scores_get(
    exam_name: str,
    class_name: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: ExportStage | None = None,
):
    """GET方式导出指定考试的成绩Excel"""
    result = export_exam_scores(db, user.id, exam_name, class_name, education_stage=education_stage)
    if isinstance(result, dict) and "error" in result:
        _export_error(result)

    cls = class_name or "全部班级"
    return register_and_respond(db, user.id, result, "成绩表", f"{cls}_{exam_name}", "xlsx")


# ── 错题条PDF ──

@router.post("/exam-export/error-sheet/student")
def api_error_sheet_by_student(
    data: ExamNameRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """按学生生成错题条PDF

    请求体:
        exam_name: str (必填) — 考试名称
        class_name: str (可选) — 班级名称
    """
    exam_name = data.exam_name
    class_name = data.class_name

    if not exam_name:
        raise HTTPException(400, detail="exam_name为必填参数")

    from services.error_sheet import (
        export_error_sheet_by_student,
        export_error_sheet_html,
    )

    result = export_error_sheet_by_student(db, user.id, exam_name, class_name, education_stage=data.education_stage)

    # 如果reportlab不可用，降级为HTML
    if isinstance(result, dict) and "error" in result:
        if "reportlab" in str(result["error"]):
            html = export_error_sheet_html(db, user.id, exam_name, class_name, mode="student", education_stage=data.education_stage)
            if isinstance(html, dict) and "error" in html:
                _export_error(html)
            return HTMLResponse(content=html)
        _export_error(result)

    cls = class_name or "全部班级"
    return register_and_respond(db, user.id, result, "错题条", f"{cls}_{exam_name}", "pdf")


@router.post("/exam-export/error-sheet/question")
def api_error_sheet_by_question(
    data: ExamNameRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """按题号生成错题汇总PDF（老师课堂讲评用）

    请求体:
        exam_name: str (必填) — 考试名称
        class_name: str (可选) — 班级名称
    """
    exam_name = data.exam_name
    class_name = data.class_name

    if not exam_name:
        raise HTTPException(400, detail="exam_name为必填参数")

    from services.error_sheet import (
        export_error_sheet_by_question,
        export_error_sheet_html,
    )

    result = export_error_sheet_by_question(db, user.id, exam_name, class_name, education_stage=data.education_stage)

    if isinstance(result, dict) and "error" in result:
        if "reportlab" in str(result["error"]):
            html = export_error_sheet_html(db, user.id, exam_name, class_name, mode="question", education_stage=data.education_stage)
            if isinstance(html, dict) and "error" in html:
                _export_error(html)
            return HTMLResponse(content=html)
        _export_error(result)

    cls = class_name or "全部班级"
    return register_and_respond(db, user.id, result, "错题汇总", f"{cls}_{exam_name}", "pdf")


@router.get("/exam-export/error-sheet/preview")
def api_error_sheet_preview(
    exam_name: str,
    class_name: Optional[str] = None,
    mode: str = "student",
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: ExportStage | None = None,
):
    """错题条HTML预览（可在浏览器中Ctrl+P打印）

    Query params:
        exam_name: str (必填)
        class_name: str (可选)
        mode: "student" | "question"
    """
    from services.error_sheet import export_error_sheet_html

    html = export_error_sheet_html(db, user.id, exam_name, class_name, mode=mode, education_stage=education_stage)
    if isinstance(html, dict) and "error" in html:
        _export_error(html)
    return HTMLResponse(content=html)
