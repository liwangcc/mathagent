"""报告导出服务 — Word/Excel导出"""

import io
import logging

from sqlalchemy.orm import Session

from models import Student, GradingResult
from services.export_text import escape_export_html, normalize_export_text
from services.scoring_compat import display_metric, exam_points, record_correct_rate
from services.class_analytics import filter_education_stage, _scoped_rows, report_scope_label, _stage_identity

logger = logging.getLogger(__name__)


def _metric_text(record) -> str:
    metric = display_metric(record)
    if metric["kind"] == "points":
        if metric["value"] is None:
            return "待复核"
        return f"{metric['value']:g}/{metric['maximum']:g}分" if metric["maximum"] else f"{metric['value']:g}分"
    return f"{metric['value']:.0%}" if metric["value"] is not None else "待复核"

try:
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


def export_student_list_excel(db: Session, teacher_id: int, *, education_stage=None) -> io.BytesIO | dict:
    """Export all students' score data as Excel."""
    if not HAS_OPENPYXL:
        return {"error": "openpyxl未安装"}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "学生成绩表"

    headers = ["姓名", "班级", "考试名称", "总题数", "正确", "错误", "成绩/正确率", "学段"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=1, column=i, value=h)

    results = (
        db.query(GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == teacher_id,
            GradingResult.deleted_at.is_(None),
            GradingResult.analytics_included.is_(True),
        )
        .order_by(Student.name, GradingResult.id)
        .all()
    )
    results = _scoped_rows(results, education_stage)
    row = 2
    for gr in results:
        student = gr.student
        ws.cell(row=row, column=1, value=student.name)
        ws.cell(row=row, column=2, value=student.class_name)
        ws.cell(row=row, column=3, value=gr.exam_name)
        ws.cell(row=row, column=4, value=gr.total_questions)
        ws.cell(row=row, column=5, value=gr.correct_count)
        ws.cell(row=row, column=6, value=gr.wrong_count)
        ws.cell(row=row, column=7, value=_metric_text(gr))
        ws.cell(row=row, column=8, value=report_scope_label(gr.education_stage or ""))
        row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_exam_scores_excel(db: Session, teacher_id: int, exam_name: str, class_name: str = None, *, education_stage=None) -> io.BytesIO | dict:
    """Export all students' scores for a specific exam as Excel.

    Columns: 排名, 学号, 姓名, 班级, [每题得分...], 总分, 正确率
    Sorted by score descending. Returns io.BytesIO.
    """
    if not HAS_OPENPYXL:
        return {"error": "openpyxl未安装"}

    # Build query for grading results matching exam_name
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
    results = filter_education_stage(q, GradingResult, education_stage).all()
    results.sort(key=lambda row: exam_points(row)[0] if exam_points(row)[0] is not None else -1, reverse=True)

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    # Determine if per-question details are available
    first_details = results[0].get_details() if results else []
    has_question_details = isinstance(first_details, list) and len(first_details) > 0 and isinstance(first_details[0], dict)

    # Build headers
    headers = ["排名", "学号", "姓名", "班级"]
    num_detail_cols = 0
    if has_question_details:
        num_detail_cols = len(first_details)
        for i, detail in enumerate(first_details, 1):
            qno = detail.get("question_no") or detail.get("question_number") or i
            headers.append(f"第{qno}题")
    headers.append("总分")
    headers.append("正确率")
    headers.append("学段")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{exam_name} 成绩表"

    # Styles
    header_font = Font(bold=True, size=11)
    center_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # Write headers
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Write data rows
    for rank, gr in enumerate(results, 1):
        student = gr.student
        row_num = rank + 1

        same_scope_rank = 1 + sum(1 for other in results[:rank - 1] if _stage_identity(other) == _stage_identity(gr))
        ws.cell(row=row_num, column=1, value=same_scope_rank).alignment = center_align
        ws.cell(row=row_num, column=2, value=student.student_no or "").alignment = center_align
        ws.cell(row=row_num, column=3, value=student.name)
        ws.cell(row=row_num, column=4, value=student.class_name).alignment = center_align

        col_offset = 5
        if has_question_details:
            details = gr.get_details()
            for i, d in enumerate(details):
                if isinstance(d, dict):
                    q_score = d.get("awarded_score", d.get("score", d.get("points", d.get("result", ""))))
                else:
                    q_score = d
                if isinstance(q_score, str):
                    q_score = normalize_export_text(q_score)
                ws.cell(row=row_num, column=col_offset + i, value=q_score).alignment = center_align
            col_offset += num_detail_cols

        # Total score
        awarded, _maximum = exam_points(gr)
        score_cell = ws.cell(
            row=row_num, column=col_offset,
            value=round(awarded, 2) if awarded is not None else None,
        )
        score_cell.alignment = center_align

        # Correct rate
        correct_rate = record_correct_rate(gr) or 0
        rate_cell = ws.cell(row=row_num, column=col_offset + 1, value=f"{correct_rate:.0%}")
        rate_cell.alignment = center_align
        ws.cell(row=row_num, column=col_offset + 2, value=report_scope_label(gr.education_stage or ""))

        # Apply borders to data cells
        for c in range(1, col_offset + 2):
            ws.cell(row=row_num, column=c).border = thin_border

    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except Exception:
                pass
        adjusted_width = min(max_length + 4, 25)
        ws.column_dimensions[col[0].column_letter].width = max(adjusted_width, 8)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_error_cards_html(db: Session, teacher_id: int, exam_name: str, class_name: str = None, *, education_stage=None) -> str | dict:
    """Generate error cards as HTML for printing as PDF.

    Each student gets a section with their wrong questions, correct answers,
    and solution steps. Uses KaTeX for math rendering.
    """
    # Keep optional PDF/plotting dependencies out of module import.  This
    # function is the actual export boundary, so resolve shared selectors here.
    from services.error_sheet import _preferred_correct_answer, _preferred_solution_steps

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
    results = filter_education_stage(q, GradingResult, education_stage).order_by(Student.class_name, Student.name).all()

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    # Collect error info per student
    student_errors = []
    for gr in results:
        student = gr.student
        details = gr.get_details()
        wrong_questions = []

        if isinstance(details, list) and details:
            for d in details:
                if not isinstance(d, dict):
                    continue
                result_status = str(d.get("result", "") or "").strip().lower()
                if result_status in ("wrong", "partial", "错误", "部分正确"):
                    wrong_questions.append({
                        "q_index": str(d.get("question_no") or d.get("question_number") or len(wrong_questions) + 1),
                        "question": d.get("question", d.get("content", "")),
                        "student_answer": d.get("student_answer", d.get("answer", "未记录")),
                        "correct_answer": _preferred_correct_answer(d),
                        "solution_steps": _preferred_solution_steps(d),
                    })

        # Fallback if no per-question details
        if not wrong_questions and gr.wrong_count > 0:
            wrong_questions.append({
                "q_index": "0",
                "question": f"（共{gr.wrong_count}题错误，详情见批改记录）",
                "student_answer": "—",
                "correct_answer": "—",
                "solution_steps": "请参考课堂讲解",
            })

        if wrong_questions:
            student_errors.append({
                "name": student.name,
                "student_no": student.student_no or "",
                "class_name": student.class_name,
                "education_stage": gr.education_stage or "",
                "score": gr.score,
                "metric_text": _metric_text(gr),
                "total": gr.total_questions,
                "correct": gr.correct_count,
                "wrong_count": gr.wrong_count,
                "errors": wrong_questions,
            })

    if not student_errors:
        return f"<html><body><h2>考试 '{_escape(exam_name)}' 没有错题记录，所有学生全部正确！</h2></body></html>"

    # Build HTML
    student_cards = []
    for s in student_errors:
        error_items = []
        for idx, err in enumerate(s["errors"], 1):
            question_no = str(err.get("q_index") or "").strip()
            question_label = (
                f"第{question_no}题"
                if question_no and question_no != "0"
                else f"第{idx}题"
            )
            sol = _solution_html(err["solution_steps"])

            error_items.append(f'''
            <div class="error-item">
                <div class="error-question"><span class="label">{_escape(question_label)}：</span>{_escape(err["question"])}</div>
                <div class="error-student-answer"><span class="label">学生答案：</span>{_escape(err["student_answer"])}</div>
                <div class="error-correct-answer"><span class="label">参考答案：</span>{_escape(err["correct_answer"])}</div>
                <div class="error-solution"><span class="label">解题步骤：</span><br>{sol}</div>
            </div>''')

        student_cards.append(f'''
    <div class="student-card">
        <div class="student-header">
            <h2>{_escape(s["name"])} 的错题条</h2>
            <div class="student-meta">
                学号：{_escape(s["student_no"])} &nbsp;|&nbsp;
                班级：{_escape(s["class_name"])} &nbsp;|&nbsp;
                学段：{_escape(report_scope_label(s["education_stage"]))} &nbsp;|&nbsp;
                {_escape(s["metric_text"])} &nbsp;|&nbsp;
                正确：{s["correct"]}/{s["total"]} &nbsp;|&nbsp;
                错误：{s["wrong_count"]}题
            </div>
        </div>
        {"".join(error_items)}
    </div>''')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>错题条 — {_escape(exam_name)}</title>
    <style>
        @page {{ size: A4; margin: 15mm 12mm; }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: "Microsoft YaHei", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
            font-size: 13px; line-height: 1.6; color: #333; padding: 0; margin: 0;
        }}
        .exam-title {{ text-align: center; font-size: 22px; font-weight: bold; margin: 20px 0 10px; color: #1a1a1a; }}
        .exam-subtitle {{ text-align: center; font-size: 13px; color: #666; margin-bottom: 20px; }}
        .student-card {{
            page-break-inside: avoid; break-inside: avoid;
            margin-bottom: 24px; border: 1px solid #ddd; border-radius: 8px;
            padding: 16px 20px; background: #fafafa;
        }}
        .student-header {{ border-bottom: 2px solid #409eff; padding-bottom: 8px; margin-bottom: 12px; }}
        .student-header h2 {{ margin: 0 0 4px 0; font-size: 16px; color: #409eff; }}
        .student-meta {{ font-size: 12px; color: #888; }}
        .error-item {{
            margin-bottom: 14px; padding: 10px 12px; background: #fff;
            border-left: 4px solid #e6a23c; border-radius: 4px;
        }}
        .error-question {{ font-weight: bold; margin-bottom: 6px; overflow-wrap: anywhere; }}
        .error-student-answer {{ color: #f56c6c; margin-bottom: 4px; }}
        .error-correct-answer {{ color: #67c23a; margin-bottom: 4px; }}
        .error-solution {{
            color: #606266; font-size: 12px; background: #f0f9eb;
            padding: 8px; border-radius: 4px;
        }}
        .label {{ font-weight: bold; color: #333; }}
        @media print {{
            .student-card {{ page-break-inside: avoid; break-inside: avoid; }}
        }}
    </style>
</head>
<body>
    <div class="exam-title">{_escape(exam_name)} — 错题条</div>
    <div class="exam-subtitle">共 {len(student_errors)} 位同学有错题 &nbsp;|&nbsp; 打印提示：Ctrl+P 选择A4纸张</div>
    {"".join(student_cards)}
</body>
</html>'''

    return html


def _escape(text: str) -> str:
    """Normalize model/OCR notation and escape HTML special characters."""
    return escape_export_html(text)


def _solution_html(steps) -> str:
    if isinstance(steps, list):
        values = [_escape(step) for step in steps if str(step or "").strip()]
        return "<br>".join(f"第{index}步：{value}" for index, value in enumerate(values, 1))
    if steps:
        return _escape(steps)
    return "（暂无解题步骤）"
