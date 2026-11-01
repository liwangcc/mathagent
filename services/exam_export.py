"""成绩导出Excel服务 — 按班级/考试导出成绩单，支持排名和逐题得分"""

import io
import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_


from models import Student, GradingResult
from services.export_text import normalize_export_text
from services.scoring_compat import exam_points, record_correct_rate

logger = logging.getLogger(__name__)


def filter_export_stage(query, education_stage: str | None = None):
    """Select archived scope explicitly, never from today's teacher profile."""
    if education_stage is None:
        return query
    if education_stage == "legacy":
        return query.filter(or_(GradingResult.education_stage == "", GradingResult.education_stage.is_(None)))
    if education_stage not in {"primary", "junior", "senior"}:
        raise ValueError("学段须为 primary、junior、senior 或 legacy")
    return query.filter(GradingResult.education_stage == education_stage)


def export_scope_conflict(results) -> dict | None:
    stages = {getattr(row, "education_stage", "") or "legacy" for row in results}
    if len(stages) > 1:
        return {"error": "同名考试包含多个学段或历史未标学段记录，请明确选择学段后导出", "code": "ambiguous_export_stage", "education_stages": sorted(stages)}
    return None

def export_exam_scores(
    db: Session,
    teacher_id: int,
    exam_name: str,
    class_name: Optional[str] = None,
    *, education_stage: str | None = None,
) -> io.BytesIO | dict:
    """导出指定考试的成绩Excel，按总分从高到低排名。

    Excel列: 排名、学号、姓名、班级、第1题得分、第2题得分、...、总分、正确率
    """
    try:
        import openpyxl
        from openpyxl.styles import (
            Alignment,
            Border,
            Font,
            PatternFill,
            Side,
        )
    except ImportError:
        return {"error": "openpyxl未安装，请执行 pip install openpyxl"}

    # 查询该考试的所有批改结果
    q = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            GradingResult.exam_name == exam_name,
            Student.teacher_id == teacher_id,
            GradingResult.deleted_at.is_(None),
            GradingResult.analytics_included.is_(True),
        )
    )
    if class_name:
        q = q.filter(Student.class_name == class_name)
    results = filter_export_stage(q, education_stage).all()
    conflict = export_scope_conflict(results)
    if conflict:
        return conflict
    results.sort(key=lambda row: exam_points(row)[0] if exam_points(row)[0] is not None else -1, reverse=True)

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    # 解析逐题得分
    first_details = results[0].get_details() if results else []
    has_details = (
        isinstance(first_details, list)
        and len(first_details) > 0
        and isinstance(first_details[0], dict)
    )
    num_questions = len(first_details) if has_details else 0

    # ── 表头 ──
    headers = ["排名", "学号", "姓名", "班级"]
    if has_details:
        for i, detail in enumerate(first_details, 1):
            qno = detail.get("question_no") or detail.get("question_number") or i
            headers.append(f"第{qno}题")
    headers.extend(["总分", "正确率"])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{exam_name} 成绩"

    # ── 样式 ──
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    center = Alignment(horizontal="center", vertical="center")
    thin = Border(
        left=Side("thin"), right=Side("thin"),
        top=Side("thin"), bottom=Side("thin"),
    )
    # 前三名高亮
    gold_fill = PatternFill(start_color="FFD700", end_color="FFD700", fill_type="solid")
    silver_fill = PatternFill(start_color="C0C0C0", end_color="C0C0C0", fill_type="solid")
    bronze_fill = PatternFill(start_color="CD7F32", end_color="CD7F32", fill_type="solid")
    rank_fills = [gold_fill, silver_fill, bronze_fill]

    # 写表头
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = thin

    # 写数据行
    for rank, gr in enumerate(results, 1):
        student = gr.student
        row = rank + 1

        ws.cell(row=row, column=1, value=rank).alignment = center
        ws.cell(row=row, column=2, value=student.student_no or "").alignment = center
        ws.cell(row=row, column=3, value=student.name)
        ws.cell(row=row, column=4, value=student.class_name).alignment = center

        col = 5
        if has_details:
            details = gr.get_details()
            for d in details:
                if isinstance(d, dict):
                    val = d.get("awarded_score", d.get("score", d.get("points", d.get("result", ""))))
                else:
                    val = d
                if isinstance(val, str):
                    val = normalize_export_text(val)
                ws.cell(row=row, column=col, value=val).alignment = center
                col += 1

        # 总分
        awarded, _maximum = exam_points(gr)
        score_val = round(awarded, 2) if awarded is not None else None
        score_cell = ws.cell(row=row, column=col, value=score_val)
        score_cell.alignment = center
        score_cell.font = Font(bold=True, size=11)

        # 正确率
        rate = record_correct_rate(gr) or 0
        rate_cell = ws.cell(row=row, column=col + 1, value=f"{rate:.0%}")
        rate_cell.alignment = center

        # 边框 + 前三名高亮
        total_cols = len(headers)
        for c in range(1, total_cols + 1):
            cell = ws.cell(row=row, column=c)
            cell.border = thin
            if rank <= 3:
                cell.fill = rank_fills[rank - 1]

    # 自动列宽
    for col_cells in ws.columns:
        max_len = 0
        for cell in col_cells:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 25)

    # 冻结首行
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def list_exams(db: Session, teacher_id: int, class_name: Optional[str] = None, *, education_stage: str | None = None) -> list[dict]:
    """列出所有有成绩记录的考试，返回 [{exam_name, class_name, student_count, avg_score}]"""
    q = (
        db.query(GradingResult, Student.class_name)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == teacher_id,
            GradingResult.deleted_at.is_(None),
            GradingResult.analytics_included.is_(True),
        )
    )
    if class_name:
        q = q.filter(Student.class_name == class_name)
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for result, row_class_name in filter_export_stage(q, education_stage).all():
        if not result.exam_name:
            continue
        awarded, _maximum = exam_points(result)
        if awarded is None:
            continue
        grouped.setdefault((result.exam_name, row_class_name or "", result.education_stage or "legacy"), []).append(awarded)
    rows = []
    for (exam_name, row_class_name, stage), scores in sorted(grouped.items(), reverse=True):
        rows.append({
            "exam_name": exam_name,
            "class_name": row_class_name,
            "education_stage": stage,
            "student_count": len(scores),
            "avg_score": round(sum(scores) / len(scores), 2),
        })
    return rows


def list_classes(db: Session, teacher_id: int, *, education_stage: str | None = None) -> list[str]:
    """列出所有有成绩记录的班级"""
    query = (
        db.query(Student.class_name)
        .join(GradingResult, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == teacher_id,
            GradingResult.deleted_at.is_(None),
            GradingResult.analytics_included.is_(True),
        )
    )
    rows = filter_export_stage(query, education_stage).distinct().all()
    return [r[0] for r in rows if r[0]]
