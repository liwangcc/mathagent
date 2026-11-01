"""报告生成接口 — 总结报告 + 学习报告"""

import asyncio
import io
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_db
from models import (
    KnowledgeEvent,
    Student,
    StudentProfile,
)
from models_user import User
from routers.auth import _require_user
from services.analyzer import generate_report_text
from services.generated_files import register_generated_file, make_display_name, register_and_respond
from services.scoring_compat import record_correct_rate
import services.class_analytics as analytics

logger = logging.getLogger(__name__)
router = APIRouter()


def _report_templates():
    import services.report_templates as module

    return module


def _export_student_list_excel(*args, **kwargs):
    from services.exporter import export_student_list_excel

    return export_student_list_excel(*args, **kwargs)


def _export_exam_scores_excel(*args, **kwargs):
    from services.exporter import export_exam_scores_excel

    return export_exam_scores_excel(*args, **kwargs)


def _export_error_cards_html(*args, **kwargs):
    from services.exporter import export_error_cards_html

    return export_error_cards_html(*args, **kwargs)


class ReportScopeRequest(BaseModel):
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None

    @field_validator("education_stage")
    @classmethod
    def normalize_scope(cls, value):
        return value or None


class SummaryReportRequest(ReportScopeRequest):
    type: str = Field("all", pattern="^(all|daily|exam)$")
    period: str = Field("week", pattern="^(week|month|all)$")
    class_id: int | None = Field(None, ge=1)
    start_date: str | None = Field(None, max_length=30)
    end_date: str = Field(default_factory=lambda: str(date.today()), max_length=30)
    class_name: str | None = Field(None, max_length=50)


class StudentReportRequest(ReportScopeRequest):
    student_id: int = Field(..., ge=1)
    format: str = Field("text", pattern="^(text|word|excel)$")
    start_date: str | None = Field(None, max_length=30)
    end_date: str = Field(default_factory=lambda: str(date.today()), max_length=30)


class ExamExportRequest(ReportScopeRequest):
    exam_name: str = Field(..., min_length=1, max_length=100)
    class_name: str | None = Field(None, max_length=50)


def _owned_student_or_404(db: Session, student_id, user: User) -> Student:
    """验证 student 属于当前老师,否则 404。"""
    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生不存在")
    return student


@router.post("/reports/summary")
def generate_summary(
    data: SummaryReportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Return a deterministic, class-scoped overview without invoking a model."""
    from models_rate_limit import TeacherClass

    student_query = db.query(Student).filter(Student.teacher_id == user.id)
    if data.class_id is not None:
        owned_class = db.query(TeacherClass).filter(
            TeacherClass.id == data.class_id,
            TeacherClass.teacher_id == user.id,
            TeacherClass.deleted_at.is_(None),
        ).first()
        if not owned_class:
            raise HTTPException(404, detail="班级不存在")
        student_query = student_query.filter(Student.class_id == data.class_id)
    elif data.class_name:
        student_query = student_query.filter(Student.class_name == data.class_name)

    students = student_query.all()
    student_map = {student.id: student for student in students}
    # Windows are rolling (now - 7/30 days), so label them that way.
    period_labels = {"week": "近7天", "month": "近30天", "all": "全部历史"}
    period_label = period_labels[data.period]

    def empty_response(message: str):
        stats = {
            "profile_count": 0,
            "total_questions": 0,
            "correct_count": 0,
            "partial_count": 0,
            "wrong_count": 0,
            "correct_rate": 0.0,
            "partial_rate": 0.0,
            "wrong_rate": 0.0,
        }
        return {
            "education_stage": data.education_stage,
            "education_stages": [],
            "summary": message,
            "top_students": [],
            "weak_students": [],
            "error_distribution": [],
            "knowledge_distribution": [],
            "unclassified_knowledge_count": 0,
            "total": 0,
            "total_questions": 0,
            "accuracy": 0.0,
            "report": message,
            "stats": stats,
        }

    if not student_map:
        return empty_response("当前班级没有学生数据")

    now = datetime.now()
    start_at = None
    if data.start_date:
        try:
            start_at = datetime.fromisoformat(data.start_date)
        except ValueError as exc:
            raise HTTPException(422, detail="start_date 格式无效") from exc
    elif data.period == "week":
        start_at = now - timedelta(days=7)
    elif data.period == "month":
        start_at = now - timedelta(days=30)

    try:
        end_at = datetime.fromisoformat(data.end_date).replace(hour=23, minute=59, second=59, microsecond=999999)
    except ValueError as exc:
        raise HTTPException(422, detail="end_date 格式无效") from exc

    profile_query = db.query(StudentProfile).filter(
        StudentProfile.student_id.in_(student_map),
        StudentProfile.analytics_included.is_(True),
        StudentProfile.created_at <= end_at,
    )
    if data.type != "all":
        profile_query = profile_query.filter(StudentProfile.category == data.type)
    if start_at is not None:
        profile_query = profile_query.filter(StudentProfile.created_at >= start_at)
    profiles = analytics.filter_education_stage(profile_query, StudentProfile, data.education_stage).order_by(StudentProfile.created_at.asc(), StudentProfile.id.asc()).all()
    if not profiles:
        return empty_response(f"{period_label}暂无正式批改记录")

    total_q = sum(int(profile.total_questions or 0) for profile in profiles)
    total_c = sum(int(profile.correct_count or 0) for profile in profiles)
    total_p = sum(int(profile.partial_count or 0) for profile in profiles)
    total_w = sum(int(profile.wrong_count or 0) for profile in profiles)
    denominator = total_q or 1
    correct_rate = total_c / denominator
    partial_rate = total_p / denominator
    wrong_rate = total_w / denominator

    grading_result_ids = [
        int(profile.grading_result_id)
        for profile in profiles
        if profile.grading_result_id is not None
    ]
    knowledge_rows: list[object] = []
    covered_result_ids: set[int] = set()
    if grading_result_ids:
        event_rows = db.query(KnowledgeEvent).filter(
            KnowledgeEvent.student_id.in_(student_map),
            KnowledgeEvent.grading_result_id.in_(grading_result_ids),
            KnowledgeEvent.analytics_included.is_(True),
        ).all()
        knowledge_rows.extend(event_rows)
        covered_result_ids = {
            int(event.grading_result_id)
            for event in event_rows
            if event.grading_result_id is not None
        }
    # Legacy archives may predate KnowledgeEvent projection. Use their
    # embedded metadata only when that result has no event, avoiding duplicates.
    for profile in profiles:
        if (
            profile.grading_result_id is not None
            and int(profile.grading_result_id) in covered_result_ids
        ):
            continue
        for detail in profile.get_details():
            if not isinstance(detail, dict):
                continue
            knowledge_rows.append({
                **analytics.row_stage_metadata(profile),
                "student_id": profile.student_id,
                "chapter": detail.get("chapter", ""),
                "topic": detail.get("topic") or detail.get("knowledge_point", ""),
                "result": detail.get("result", ""),
                "question_bank_id": detail.get("question_bank_id"),
            })
    knowledge_distribution = analytics.knowledge_distribution_for_rows(db, knowledge_rows, data.education_stage)
    unclassified_knowledge_count = sum(
        int(item.get("count") or 0)
        for item in knowledge_distribution
        if item.get("topic") == "未分类"
    )

    student_totals = defaultdict(lambda: {
        "profile_count": 0,
        "total_questions": 0,
        "correct_count": 0,
        "partial_count": 0,
        "wrong_count": 0,
    })
    for profile in profiles:
        aggregate = student_totals[profile.student_id]
        aggregate["profile_count"] += 1
        aggregate["total_questions"] += int(profile.total_questions or 0)
        aggregate["correct_count"] += int(profile.correct_count or 0)
        aggregate["partial_count"] += int(profile.partial_count or 0)
        aggregate["wrong_count"] += int(profile.wrong_count or 0)

    student_rows = []
    for student_id, aggregate in student_totals.items():
        student = student_map.get(student_id)
        if not student:
            continue
        student_total = aggregate["total_questions"] or 1
        accuracy = aggregate["correct_count"] / student_total
        student_rows.append({
            "id": student.id,
            "name": student.name,
            "student_no": student.student_no or "",
            "profile_count": aggregate["profile_count"],
            "total_questions": aggregate["total_questions"],
            "accuracy": round(accuracy * 100, 1),
        })

    # The two lists are disjoint: in a small class the strongest students must
    # never reappear under "needs attention". Top takes at most half the class.
    ranked = sorted(
        student_rows,
        key=lambda item: (-item["accuracy"], -item["total_questions"], item["student_no"]),
    )
    top_students = ranked[:min(5, (len(ranked) + 1) // 2)]
    top_ids = {item["id"] for item in top_students}
    weak_students = sorted(
        (item for item in student_rows if item["id"] not in top_ids),
        key=lambda item: (item["accuracy"], -item["total_questions"], item["student_no"]),
    )[:5]
    # Semantic error-cause distribution is retired; reports are knowledge-point only.
    error_distribution = []
    summary = (
        f"{period_label}共完成 {len(profiles)} 份正式批改，覆盖 {len(student_rows)} 名学生、"
        f"{total_q} 道作答；正确 {total_c} 道，部分正确 {total_p} 道，错误 {total_w} 道，"
        f"整体正确率 {correct_rate:.1%}。"
    )
    stats = {
        "profile_count": len(profiles),
        "total_questions": total_q,
        "correct_count": total_c,
        "partial_count": total_p,
        "wrong_count": total_w,
        "correct_rate": round(correct_rate, 4),
        "partial_rate": round(partial_rate, 4),
        "wrong_rate": round(wrong_rate, 4),
    }
    return {
        "summary": summary,
        "education_stage": data.education_stage,
        "education_stages": sorted({profile.education_stage or "" for profile in profiles}),
        "top_students": top_students,
        "weak_students": weak_students,
        "error_distribution": error_distribution,
        "knowledge_distribution": knowledge_distribution,
        "unclassified_knowledge_count": unclassified_knowledge_count,
        "total": len(profiles),
        "total_questions": total_q,
        "accuracy": round(correct_rate * 100, 1),
        "report": summary,
        "stats": stats,
    }


@router.post("/reports/student")
async def generate_student_report(
    data: StudentReportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Generate a learning report for a specific student."""
    student_id = data.student_id
    report_format = data.format
    start_date = data.start_date
    end_date = data.end_date

    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生不存在")

    q = db.query(StudentProfile).filter(
        StudentProfile.student_id == student_id,
        StudentProfile.analytics_included.is_(True),
    )
    if start_date:
        q = q.filter(StudentProfile.created_at >= start_date)
    q = q.filter(StudentProfile.created_at <= end_date + "T23:59:59")
    profiles = analytics.filter_education_stage(q, StudentProfile, data.education_stage).order_by(StudentProfile.created_at.asc()).all()

    if not profiles:
        return {"report": f"{student.name}在指定时间段内暂无数据"}

    # Build data summary
    scores = [rate for p in profiles if (rate := record_correct_rate(p)) is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    if len(scores) < 2:
        trend = "数据不足"
    elif scores[-1] > scores[0]:
        trend = "上升"
    elif scores[-1] < scores[0]:
        trend = "下降"
    else:
        trend = "持平"

    all_errors = []
    for p in profiles:
        for d in p.get_details():
            if d.get("result") in ("partial", "wrong"):
                all_errors.append({
                    "question": d.get("question", ""),
                    "student_answer": d.get("student_answer", ""),
                    "correct_answer": d.get("correct_answer", ""),
                    "result": d.get("result"),
                })

    stages = {profile.education_stage or "" for profile in profiles}
    stage_name = {"primary": "小学", "junior": "初中", "senior": "高中", "": ""}.get(next(iter(stages)), "") if len(stages) == 1 else ""
    if len(stages) > 1:
        trend = "跨学段资料，不比较成绩升降"
    prompt = f"""你是一位{stage_name}数学老师。以下是学生{student.name}的学习数据：
统计范围：{analytics.report_scope_label(data.education_stage)}。未标学段资料不得猜测为小学、初中或高中；不同学段不得比较难度或成绩升降。

历次成绩：{', '.join(f'{s:.0%}' for s in scores)}
平均得分率：{avg_score:.1%}
成绩趋势：{trend}
错题数：{len(all_errors)}

错题记录：
{chr(10).join(f"- {e['question'][:100]}" for e in all_errors[:10])}

请生成一份面向家长/学生的学习报告，包含：
1. 基本学习情况概述
2. 成绩趋势分析
3. 主要薄弱知识点及错题解析
4. 学习建议
语言通俗易懂，家长和学生都能看懂。200-300字。"""

    try:
        report_text = await generate_report_text(
            [{"role": "user", "content": prompt}],
            teacher_id=str(user.id),
        )
    except Exception:
        report_text = "LLM生成报告失败，请稍后重试"

    if report_format == "text":
        return {
            "report": report_text,
            "education_stage": data.education_stage,
            "education_stages": sorted(stages),
            "stats": {
                "student": student.to_dict(),
                "profile_count": len(profiles),
                "average_score": round(avg_score, 2),
                "trend": trend,
                "error_count": len(all_errors),
            },
        }

    # Word format uses the same designed campus-style template as the class module.
    if report_format == "word":
        buf = await asyncio.to_thread(
            _report_templates().build_student_report,
            db,
            student_id,
            user.id,
            education_stage=data.education_stage,
        )
        if isinstance(buf, dict) and "error" in buf:
            raise HTTPException(500, detail=buf["error"])
        return register_and_respond(db, user.id, buf, "个人报告", student.name, "docx")

    if report_format == "excel":
        buf = await asyncio.to_thread(_export_student_list_excel, db, user.id, education_stage=data.education_stage)
        if isinstance(buf, dict) and "error" in buf:
            raise HTTPException(500, detail=buf["error"])
        # This workbook lists every student; never label it as one student's file.
        return register_and_respond(db, user.id, buf, "学生成绩表", "全部学生", "xlsx")

    return {"report": report_text, "message": f"格式 {report_format} 暂不支持，返回文本格式"}


@router.post("/report/regular/{student_id}")
def generate_regular_report(
    student_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """生成普通批改报告Word文档"""
    student = _owned_student_or_404(db, student_id, user)
    result = _report_templates().build_student_report(db, student_id, user.id, education_stage=education_stage or None)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(500, detail=result["error"])
    return register_and_respond(db, user.id, result, "普通批改报告", student.name, "docx")


@router.post("/report/summary/{student_id}")
def generate_summary_word(
    student_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """生成学习总结Word文档"""
    student = _owned_student_or_404(db, student_id, user)
    result = _report_templates().build_student_report(db, student_id, user.id, education_stage=education_stage or None)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(500, detail=result["error"])
    return register_and_respond(db, user.id, result, "学习总结", student.name, "docx")


@router.get("/report/excel/export")
def export_excel_report(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """导出所有学生成绩Excel"""
    result = _export_student_list_excel(db, user.id, education_stage=education_stage or None)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(500, detail=result["error"])
    return register_and_respond(db, user.id, result, "学生成绩表", "全部学生", "xlsx")


@router.post("/reports/export-scores")
async def export_scores(
    data: ExamExportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """导出指定考试的成绩Excel"""
    exam_name = data.exam_name
    class_name = data.class_name
    if not exam_name:
        raise HTTPException(400, detail="exam_name为必填参数")

    result = await asyncio.to_thread(
        _export_exam_scores_excel,
        db,
        user.id,
        exam_name,
        class_name,
        education_stage=data.education_stage,
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, detail=result["error"])

    label = f"{class_name or '全部班级'}_{exam_name}"
    return register_and_respond(db, user.id, result, "成绩表", label, "xlsx")


@router.post("/reports/export-error-cards")
async def export_error_cards(
    data: ExamExportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """导出错题条HTML（可直接Ctrl+P打印为PDF）"""
    exam_name = data.exam_name
    class_name = data.class_name
    if not exam_name:
        raise HTTPException(400, detail="exam_name为必填参数")

    result = await asyncio.to_thread(
        _export_error_cards_html,
        db,
        user.id,
        exam_name,
        class_name,
        education_stage=data.education_stage,
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, detail=result["error"])

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=result)


# ────────────────────── 班级管理模块报告（契约 7.5）──────────────────────

class StudentReportTypeRequest(ReportScopeRequest):
    type: str = Field("student", pattern="^(student|parent_letter)$")


class BatchReportRequest(ReportScopeRequest):
    type: str = Field("student", pattern="^(student|parent_letter)$")
    lock: str = Field("token", pattern="^(token|none)$")
    expire_days: int = Field(30, ge=1, le=365)
    refresh: bool = False


def _result_or_404(result):
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, detail=result["error"])
    return result


def _safe_name(s: str) -> str:
    """文件名安全化：去路径分隔与控制字符。"""
    import re
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "").strip() or "report"


def _download_response(rec, display_name: str, *, message: str, generated: int = 1, skipped=None):
    skipped = skipped or []
    return {
        "ok": True,
        "mode": "download",
        "file_id": rec.id,
        "display_name": display_name,
        "download_url": f"/api/download/{rec.id}",
        "message": message,
        "generated": generated,
        "skipped": skipped,
        "error": None,
    }


@router.post("/class/{class_id}/report")
def gen_class_report(class_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user), education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None):
    """班级报告（校园风 docx）→ 返回 file_id + download_url。"""
    from models_rate_limit import TeacherClass
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id, TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not tc:
        raise HTTPException(404, detail="班级不存在")
    education_stage = education_stage or None
    buf = _result_or_404(_report_templates().build_class_report(db, class_id, user.id, education_stage=education_stage))
    name = make_display_name("班级报告", tc.class_name, "docx")
    rec = register_generated_file(db, user.id, buf.getvalue(), name)
    db.commit()
    return {**_download_response(rec, name, message="班级报告已生成"), "education_stage": education_stage,
            "scope_label": analytics.report_scope_label(education_stage)}


@router.post("/student/{student_id}/report")
def gen_student_report(student_id: int, req: StudentReportTypeRequest,
                       db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """学生个人报告 / 致家长信（docx）→ 返回 file_id + download_url。"""
    student = _owned_student_or_404(db, student_id, user)
    if req.type == "parent_letter":
        buf = _result_or_404(_report_templates().build_parent_letter(
            db, student_id, user.id, teacher_name=user.name or "", school=user.school or "", education_stage=req.education_stage))
    else:
        buf = _result_or_404(_report_templates().build_student_report(db, student_id, user.id, education_stage=req.education_stage))
    kind = "个人报告" if req.type == "student" else "致家长信"
    name = make_display_name(kind, student.name, "docx")
    rec = register_generated_file(db, user.id, buf.getvalue(), name)
    db.commit()
    return {**_download_response(rec, name, message=f"{kind}已生成"), "education_stage": req.education_stage,
            "scope_label": analytics.report_scope_label(req.education_stage)}


@router.post("/class/{class_id}/report/batch")
def gen_batch_reports(class_id: int, req: BatchReportRequest,
                      db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """批量生成班级全员报告。

    - 致家长信 + lock=token：为每个学生生成不可猜的群发链接（不打 ZIP）。
      有手机号时使用后四位校验；无手机号时照常生成链接，但不设置密码。
    - 其余情况：沿用打包 ZIP → file_id + download_url。
    """
    import zipfile
    from models_rate_limit import TeacherClass
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id, TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not tc:
        raise HTTPException(404, detail="班级不存在")
    # Keep the class-level authorization check above and repeat the teacher
    # ownership predicate here so inconsistent legacy rows cannot cross tenants.
    students = db.query(Student).filter(
        Student.class_id == class_id,
        Student.teacher_id == user.id,
    ).all()
    if not students:
        raise HTTPException(400, detail="班级暂无学生")

    # ── 群发链接模式（致家长信 + token 锁）──
    if req.type == "parent_letter" and req.lock == "token":
        return _gen_letter_shares(
            db,
            tc,
            students,
            user,
            req.expire_days,
            refresh=req.refresh,
            education_stage=req.education_stage,
        )

    builder = _report_templates().STUDENT_BUILDERS[req.type]
    kind = "个人报告" if req.type == "student" else "致家长信"
    zbuf = io.BytesIO()
    generated, skipped = 0, []
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in students:
            if req.type == "parent_letter":
                r = builder(db, s.id, user.id, teacher_name=user.name or "", school=user.school or "", education_stage=req.education_stage)
            else:
                r = builder(db, s.id, user.id, education_stage=req.education_stage)
            if isinstance(r, dict) and "error" in r:
                skipped.append(s.name)
                continue
            fn = f"{_safe_name(s.student_no or '')}_{_safe_name(s.name)}_{kind}.docx".lstrip("_")
            zf.writestr(fn, r.getvalue())
            generated += 1
    if generated == 0:
        raise HTTPException(500, detail="批量生成失败")
    zbuf.seek(0)
    zip_name = make_display_name(f"{kind}_批量", tc.class_name, "zip")
    rec = register_generated_file(db, user.id, zbuf.getvalue(), zip_name)
    db.commit()
    return _download_response(
        rec,
        zip_name,
        message=f"已生成 {generated} 份{kind}",
        generated=generated,
        skipped=skipped,
    )


def _gen_letter_shares(db, tc, students, user, expire_days: int, *, refresh: bool = False, education_stage=None):
    """为班级全员准备带锁链接；默认复用有效链接，显式刷新才替换。"""
    import secrets
    from datetime import datetime, timedelta
    from models import ParentLetterShare
    from services.teaching_context import make_teaching_context, require_teacher_stage

    stage = "" if education_stage == "legacy" else (education_stage or require_teacher_stage(user))
    context = make_teaching_context(stage) if stage else {}
    version = context.get("curriculum_version", "")

    now = datetime.now()
    expires_at = now + timedelta(days=expire_days)
    existing_rows = db.query(ParentLetterShare).filter(
        ParentLetterShare.class_id == tc.id,
        ParentLetterShare.teacher_id == user.id,
        ParentLetterShare.revoked_at.is_(None),
        ParentLetterShare.education_stage == stage,
        ParentLetterShare.catalog_version == version,
    ).order_by(ParentLetterShare.created_at.desc(), ParentLetterShare.id.desc()).all()
    existing_by_student = defaultdict(list)
    for row in existing_rows:
        if row.is_valid:
            existing_by_student[row.student_id].append(row)

    from services.report_templates import letter_has_content

    shares, skipped, empty_content = [], [], []
    created_count = 0
    reused_count = 0
    extended_count = 0
    for s in students:
        if not letter_has_content(db, s.id, stage):
            empty_content.append(s.name)
        digits = "".join(ch for ch in (s.phone or "") if ch.isdigit())
        phone_last4 = digits[-4:] if len(digits) >= 4 else ""
        valid_rows = existing_by_student.get(s.id, [])
        reusable = next(
            (row for row in valid_rows if row.phone_last4 == phone_last4),
            None,
        )
        if reusable is not None and not refresh:
            for duplicate in valid_rows:
                if duplicate.id != reusable.id:
                    duplicate.revoked_at = now
            # Re-sending a link must give parents the full validity the
            # teacher just chose, not the remainder of the old window.
            if reusable.expires_at is not None and reusable.expires_at < expires_at:
                reusable.expires_at = expires_at
                extended_count += 1
            shares.append({
                **reusable.to_dict(),
                "student_name": s.name,
                "student_no": s.student_no or "",
                "reused": True,
            })
            reused_count += 1
            continue

        html = _report_templates().render_parent_letter_html(
            db, s.id, user.id, teacher_name=user.name or "", school=user.school or "", education_stage=stage)
        if isinstance(html, dict) and "error" in html:
            skipped.append(s.name)
            continue
        for old_share in valid_rows:
            old_share.revoked_at = now
        token = secrets.token_urlsafe(32)
        rec = ParentLetterShare(
            token=token, student_id=s.id, teacher_id=user.id, class_id=tc.id,
            phone_last4=phone_last4, html=html,
            education_stage=stage, catalog_version=version,
            display_name=f"{s.name} 同学家长信", expires_at=expires_at,
        )
        db.add(rec)
        db.flush()
        shares.append({
            **rec.to_dict(),
            "student_name": s.name,
            "student_no": s.student_no or "",
            "reused": False,
        })
        created_count += 1
    if not shares:
        db.rollback()
        raise HTTPException(400, detail="没有成功生成家长信链接，请检查学生档案和批阅记录")
    db.commit()
    unprotected_count = sum(
        1 for share in shares if not share.get("password_required")
    )
    message = f"已准备 {len(shares)} 个家长专属链接"
    message += f"；范围：{analytics.report_scope_label(stage)}（生成时冻结）"
    if reused_count:
        message += f"（复用 {reused_count} 个，新生成 {created_count} 个）"
    message += f"；有效期至 {expires_at:%Y-%m-%d}"
    if unprotected_count:
        message += f"；其中 {unprotected_count} 个未设置手机号校验"
    if empty_content:
        message += f"；{len(empty_content)} 名学生暂无批阅或考试数据，信件仅含问候语"
    return {
        "ok": True,
        "mode": "share",
        "education_stage": stage,
        "catalog_version": version,
        "scope_label": analytics.report_scope_label(stage),
        "message": message,
        "generated": len(shares),
        "created": created_count,
        "reused": reused_count,
        "unprotected": unprotected_count,
        "extended": extended_count,
        "empty_content": empty_content,
        "skipped": skipped,
        "shares": shares,
        "expire_days": expire_days,
        "error": None,
    }


@router.get("/class/{class_id}/letter-shares")
def list_letter_shares(class_id: int, db: Session = Depends(get_db),
                       user: User = Depends(_require_user), education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None):
    """列出本班家长信群发链接及状态（老师端管理）。"""
    from models import ParentLetterShare
    from models_rate_limit import TeacherClass
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id, TeacherClass.teacher_id == user.id,
    ).first()
    if not tc:
        raise HTTPException(404, detail="班级不存在")
    query = db.query(ParentLetterShare).filter(
        ParentLetterShare.class_id == class_id,
        ParentLetterShare.teacher_id == user.id,
    )
    education_stage = education_stage or None
    rows = analytics.filter_education_stage(query, ParentLetterShare, education_stage).order_by(ParentLetterShare.created_at.desc()).all()
    student_ids = {row.student_id for row in rows}
    students = {
        student.id: student
        for student in db.query(Student).filter(Student.id.in_(student_ids)).all()
    } if student_ids else {}
    shares = []
    for row in rows:
        student = students.get(row.student_id)
        shares.append({
            **row.to_dict(),
            "student_name": student.name if student else "学生已移除",
            "student_no": (student.student_no or "") if student else "",
        })
    return {"shares": shares, "education_stage": education_stage, "scope_label": analytics.report_scope_label(education_stage)}


@router.post("/letter-share/{share_id}/revoke")
def revoke_letter_share(share_id: int, db: Session = Depends(get_db),
                        user: User = Depends(_require_user)):
    """手动失效一条群发链接（归属校验）。"""
    from datetime import datetime
    from models import ParentLetterShare
    rec = db.query(ParentLetterShare).filter(
        ParentLetterShare.id == share_id,
        ParentLetterShare.teacher_id == user.id,
    ).first()
    if not rec:
        raise HTTPException(404, detail="链接不存在")
    if rec.revoked_at is None:
        rec.revoked_at = datetime.now()
        db.commit()
    return {"ok": True, "status": rec.status_label()}
