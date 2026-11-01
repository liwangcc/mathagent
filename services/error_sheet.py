"""错题条PDF生成服务 — 支持按学生和按题号两种模式

使用 reportlab 生成 PDF，matplotlib.mathtext 渲染 LaTeX 公式。
"""

import io
import logging
import re
from datetime import date
from typing import Optional
from services.exam_export import filter_export_stage, export_scope_conflict

from sqlalchemy.orm import Session

from models import Student, GradingResult
from services.export_text import decode_export_text, escape_export_html, normalize_export_text
from services.scoring_compat import display_metric

logger = logging.getLogger(__name__)


def _grading_metric_text(record) -> str:
    metric = display_metric(record)
    if metric["kind"] == "points":
        if metric["value"] is None:
            return "得分：待复核"
        if metric["maximum"]:
            return f"得分：{metric['value']:g}/{metric['maximum']:g}分"
        return f"得分：{metric['value']:g}分"
    if metric["value"] is None:
        return "正确率：待复核"
    return f"正确率：{metric['value']:.1%}"


def _student_metric_text(student_entry: dict) -> str:
    if student_entry.get("metric_text"):
        return str(student_entry["metric_text"])
    try:
        score = float(student_entry.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return f"得分：{score:.1f}"

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        PageBreak, Image as RLImage,
    )
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ── 中文字体注册 ──
# STSong-Light is a built-in Adobe CJK font and works when the server has no
# system Chinese font. Prefer a local TrueType font when one is available.
_CN_FONT = "Helvetica"
_CN_FONT_BOLD = "Helvetica-Bold"

if HAS_REPORTLAB:
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _CN_FONT = "STSong-Light"
        _CN_FONT_BOLD = "STSong-Light"
    except Exception as e:
        logger.debug("注册内置中文字体失败: %s", e)

def _register_cn_font():
    """尝试注册系统中文字体"""
    global _CN_FONT, _CN_FONT_BOLD
    import os
    candidates = [
        # Windows
        ("C:/Windows/Fonts/msyh.ttc", "MsYaHei"),
        ("C:/Windows/Fonts/simsun.ttc", "SimSun"),
        ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
        # Linux
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "WenQuanYi"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "NotoSansCJK"),
        # macOS
        ("/System/Library/Fonts/STHeiti Medium.ttc", "STHeiti"),
    ]
    for fpath, fname in candidates:
        if os.path.exists(fpath):
            try:
                pdfmetrics.registerFont(TTFont(fname, fpath))
                _CN_FONT = fname
                _CN_FONT_BOLD = fname
                return
            except Exception as e:
                logger.debug(f"注册字体 {fpath} 失败: {e}")

_register_cn_font()


# ── LaTeX 渲染 ──

def _render_latex_to_image(latex_str: str, fontsize: int = 14, dpi: int = 150) -> Optional[io.BytesIO]:
    """将 LaTeX 字符串渲染为 PNG 图片的 BytesIO。失败返回 None。"""
    if not HAS_MPL:
        return None
    try:
        # 确保有 $ 包裹
        text = decode_export_text(latex_str).strip()
        if not text.startswith("$"):
            text = f"${text}$"

        # Size the canvas from the formula length. A fixed wide canvas leaves
        # most of the image blank and makes short inline formulas look tiny.
        canvas_width = min(8.0, max(1.4, 0.16 * len(text) + 0.8))
        fig = figure.Figure(figsize=(canvas_width, 0.5))
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.set_axis_off()
        ax.text(
            0.02, 0.5, text, fontsize=fontsize,
            transform=ax.transAxes, verticalalignment="center",
            usetex=False,  # 用 mathtext 而非 full LaTeX
        )
        fig.tight_layout(pad=0.1)

        buf = io.BytesIO()
        canvas.print_figure(buf, format="png", dpi=dpi, bbox_inches="tight")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.debug(f"LaTeX渲染失败: {e}, 原文: {latex_str}")
        return None


def _text_with_latex(text: str, style) -> list:
    """将含 LaTeX 公式的文本拆分为 Paragraph 和 Image 的混合列表。

    如果有 LaTeX 公式，渲染为图片嵌入；否则直接返回 Paragraph。
    """
    if not text:
        return [Paragraph("（无内容）", style)]

    # 查找 $...$ 或 $$...$$ 公式；其余内容作为普通文本输出。
    text = decode_export_text(text)
    normalized_text = _normalize_math_text(text)
    if "\\" not in normalized_text:
        return [Paragraph(_escape_xml(normalized_text), style)]
    parts = re.split(r'(\$\$[\s\S]+?\$\$|\$[^$\n]+\$)', text)
    elements = []
    for part in parts:
        if not part.strip():
            continue
        if part.startswith("$"):
            normalized = _normalize_math_text(part)
            # Common OCR/model notation is clearer at the document font size
            # than as a tiny raster image. Keep image rendering for commands
            # that the conservative text normalizer cannot represent.
            if "\\" not in normalized:
                elements.append(Paragraph(_escape_xml(normalized), style))
                continue
            img_buf = _render_latex_to_image(part)
            if img_buf:
                try:
                    raw_width, raw_height = ImageReader(img_buf).getSize()
                    image_height = 16
                    image_width = raw_width / max(raw_height, 1) * image_height
                    max_width = 460
                    if image_width > max_width:
                        image_height *= max_width / image_width
                        image_width = max_width
                    img = RLImage(img_buf, width=image_width, height=image_height)
                    img.hAlign = "LEFT"
                    elements.append(img)
                except Exception:
                    elements.append(Paragraph(_escape_xml(_normalize_math_text(part)), style))
            else:
                elements.append(Paragraph(_escape_xml(_normalize_math_text(part)), style))
        else:
            display = _normalize_math_text(part)
            if display:
                elements.append(Paragraph(_escape_xml(display), style))

    return elements if elements else [Paragraph("（无内容）", style)]


def _escape_xml(text: str) -> str:
    """Escape XML special characters for reportlab Paragraph."""
    if not text:
        return ""
    return (
        decode_export_text(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _normalize_math_text(text: object) -> str:
    """Make common model/OCR math notation readable without evaluating it."""
    return normalize_export_text(text)


def _preferred_solution_steps(detail: dict) -> object:
    """Choose the most complete explanation available for an error export.

    ``model_answer`` is the detailed grader solution and is intentionally
    preferred over the frozen answer-key steps.  Older records may not have
    it, so retain deterministic fallbacks for historical exports.
    """
    for key in ("model_answer", "solution_steps", "hint", "explanation"):
        value = detail.get(key) if isinstance(detail, dict) else None
        if isinstance(value, list):
            cleaned = [item for item in value if str(item or "").strip()]
            if cleaned:
                return cleaned
        elif value is not None and str(value).strip():
            return value
    return ""


def _preferred_correct_answer(detail: dict) -> object:
    """Return the first non-empty final-answer field across old schemas."""
    for key in ("correct_answer", "standard_answer", "expected_answer"):
        value = detail.get(key) if isinstance(detail, dict) else None
        if value is not None and str(value).strip():
            return value
    return ""


def _table_cell(value: object, style) -> Paragraph:
    """Wrap table values in Paragraphs so XML escaping is interpreted."""
    return Paragraph(_escape_xml(_normalize_math_text(value)), style)


# ── 样式定义 ──

def _make_styles():
    """创建PDF使用的各种样式"""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "CNTitle", parent=styles["Title"],
        fontName=_CN_FONT_BOLD, fontSize=16, leading=22,
        spaceAfter=6 * mm, alignment=1,  # center
    ))
    styles.add(ParagraphStyle(
        "CNSubtitle", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=10, leading=14,
        spaceAfter=4 * mm, alignment=1, textColor=HexColor("#666666"),
    ))
    styles.add(ParagraphStyle(
        "StudentName", parent=styles["Heading2"],
        fontName=_CN_FONT_BOLD, fontSize=13, leading=18,
        spaceBefore=4 * mm, spaceAfter=2 * mm,
        textColor=HexColor("#2c3e50"),
    ))
    styles.add(ParagraphStyle(
        "StudentMeta", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=9, leading=13,
        spaceAfter=3 * mm, textColor=HexColor("#888888"),
    ))
    styles.add(ParagraphStyle(
        "QLabel", parent=styles["Normal"],
        fontName=_CN_FONT_BOLD, fontSize=10, leading=14,
        spaceBefore=2 * mm, spaceAfter=1 * mm,
        textColor=HexColor("#34495e"),
    ))
    styles.add(ParagraphStyle(
        "Body", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=10, leading=15,
        spaceAfter=2 * mm,
    ))
    styles.add(ParagraphStyle(
        "WrongAnswer", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=10, leading=15,
        textColor=HexColor("#e74c3c"), spaceAfter=1 * mm,
    ))
    styles.add(ParagraphStyle(
        "CorrectAnswer", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=10, leading=15,
        textColor=HexColor("#27ae60"), spaceAfter=1 * mm,
    ))
    styles.add(ParagraphStyle(
        "Solution", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=9, leading=13,
        leftIndent=10, spaceAfter=2 * mm,
        textColor=HexColor("#555555"),
        backColor=HexColor("#f0f9eb"),
    ))
    styles.add(ParagraphStyle(
        "QuestionSummary", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=9, leading=13,
        textColor=HexColor("#555555"),
    ))
    styles.add(ParagraphStyle(
        "TableHeader", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=9, leading=12,
        textColor=white, alignment=1,
    ))
    styles.add(ParagraphStyle(
        "TableCell", parent=styles["Normal"],
        fontName=_CN_FONT, fontSize=9, leading=12,
        textColor=HexColor("#333333"),
    ))
    return styles


# ── 核心逻辑 ──

def _collect_errors(results) -> list[dict]:
    """从批改结果中提取错题信息"""
    student_errors = []
    for gr in results:
        student = gr.student
        details = gr.get_details()
        wrong_questions = []

        if isinstance(details, list) and details:
            for idx, d in enumerate(details, 1):
                if not isinstance(d, dict):
                    continue
                status = str(d.get("result", "") or "").strip().lower()
                if status in ("wrong", "partial", "错误", "部分正确"):
                    wrong_questions.append({
                        "q_index": str(d.get("question_no") or d.get("question_number") or idx),
                        "question": d.get("question", d.get("content", "")),
                        "student_answer": d.get("student_answer", d.get("answer", "未记录")),
                        "correct_answer": _preferred_correct_answer(d),
                        "solution_steps": _preferred_solution_steps(d),
                        "status": status,
                    })

        # fallback: 有错误但没有逐题详情
        if not wrong_questions and gr.wrong_count > 0:
            wrong_questions.append({
                "q_index": "0",
                "question": f"（共{gr.wrong_count}题错误，详情见批改记录）",
                "student_answer": "—",
                "correct_answer": "—",
                "solution_steps": "请参考课堂讲解",
                "status": "wrong",
            })

        if wrong_questions:
            student_errors.append({
                "student_id": student.id,
                "name": student.name,
                "student_no": student.student_no or "",
                "class_name": student.class_name,
                "score": gr.score,
                "metric_text": _grading_metric_text(gr),
                "total": gr.total_questions,
                "correct": gr.correct_count,
                "wrong_count": gr.wrong_count,
                "errors": wrong_questions,
            })

    return student_errors


def _render_solution_steps(steps) -> str:
    """将解题步骤渲染为可读文本"""
    if isinstance(steps, list):
        return "<br/>".join(
            f"第{i + 1}步：{_escape_xml(_normalize_math_text(s))}"
            for i, s in enumerate(steps)
        )
    elif isinstance(steps, str) and steps:
        lines = [line.strip() for line in steps.splitlines() if line.strip()]
        return "<br/>".join(_escape_xml(_normalize_math_text(line)) for line in lines)
    return "（暂无解题步骤）"


def _build_student_card(s: dict, styles) -> list:
    """构建单个学生的错题卡片元素"""
    elements = []

    # 学生标题
    elements.append(Paragraph(
        f'<font color="#2980b9">{_escape_xml(s["name"])} 的错题条</font>',
        styles["StudentName"],
    ))
    elements.append(Paragraph(
        f'学号：{_escape_xml(s["student_no"])}  |  '
        f'班级：{_escape_xml(s["class_name"])}  |  '
        f'{_escape_xml(_student_metric_text(s))}  |  '
        f'正确：{s["correct"]}/{s["total"]}  |  '
        f'错误：{s["wrong_count"]}题',
        styles["StudentMeta"],
    ))

    # 每道错题
    for err in s["errors"]:
        q_index = str(err.get("q_index") or "").strip()
        q_label = f"第{q_index}题" if q_index and q_index != "0" else "错题"

        # 题目
        q_elements = _text_with_latex(err["question"], styles["Body"])
        label = Paragraph(f'<font color="#e67e22">题目：{_escape_xml(q_label)}</font>', styles["QLabel"])

        # 用 Table 包裹，左边橙色竖条
        rows_data = []
        rows_data.append([label])
        rows_data.extend([[e] for e in q_elements])
        rows_data.append([
            Paragraph(f'<font color="#e74c3c">学生答案：</font>{_escape_xml(_normalize_math_text(err["student_answer"]))}',
                       styles["WrongAnswer"])
        ])
        rows_data.append([
            Paragraph(f'<font color="#27ae60">参考答案：</font>{_escape_xml(_normalize_math_text(err["correct_answer"]))}',
                       styles["CorrectAnswer"])
        ])
        rows_data.append([
            Paragraph(f'解题步骤：<br/>{_render_solution_steps(err["solution_steps"])}',
                       styles["Solution"])
        ])

        tbl = Table(rows_data, colWidths=[460])
        tbl.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBEFOREDECOR", (0, 0), (0, -1), 3, HexColor("#e67e22")),
            ("BACKGROUND", (0, -1), (-1, -1), HexColor("#f0f9eb")),
        ]))
        elements.append(tbl)
        elements.append(Spacer(1, 3 * mm))

    return elements


def _build_question_summary(q_index: object, question_text: str, error_list: list, styles) -> list:
    """按题号汇总：一道题所有学生的错误"""
    elements = []

    elements.append(Paragraph(
        f'<font color="#8e44ad">第{q_index}题 — 全班错题汇总（共{len(error_list)}人出错）</font>',
        styles["StudentName"],
    ))

    # 题目
    q_elements = _text_with_latex(question_text, styles["Body"])
    elements.extend(q_elements)

    # 参考答案与完整解题步骤：优先使用首个非空值，避免首条旧记录为空时
    # 把后续学生已有的完整 model_answer 丢掉。
    if error_list:
        ca = next(
            (item.get("correct_answer", "") for item in error_list
             if str(item.get("correct_answer", "") or "").strip()),
            "",
        )
        if ca:
            elements.append(Paragraph(
                f'<font color="#27ae60">参考答案：</font>{_escape_xml(_normalize_math_text(ca))}',
                styles["CorrectAnswer"],
            ))
        sol = next(
            (item.get("solution_steps", "") for item in error_list
             if item.get("solution_steps")),
            "",
        )
        if sol:
            elements.append(Paragraph(
                f'解题步骤：<br/>{_render_solution_steps(sol)}',
                styles["Solution"],
            ))

    elements.append(Spacer(1, 2 * mm))

    # 各学生错误答案列表
    rows = [[
        _table_cell("学生姓名", styles["TableHeader"]),
        _table_cell("学号", styles["TableHeader"]),
        _table_cell("学生答案", styles["TableHeader"]),
        _table_cell("判定", styles["TableHeader"]),
    ]]
    for err in error_list:
        status_text = "错误" if err["status"] in ("wrong", "错误") else "部分正确"
        rows.append([
            _table_cell(err["student_name"], styles["TableCell"]),
            _table_cell(err["student_no"], styles["TableCell"]),
            _table_cell(err["student_answer"], styles["TableCell"]),
            _table_cell(status_text, styles["TableCell"]),
        ])

    tbl = Table(rows, colWidths=[80, 80, 220, 60])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _CN_FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#8e44ad")),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, HexColor("#f5f0ff")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(tbl)
    elements.append(Spacer(1, 5 * mm))

    return elements


def _page_header_footer(canvas, doc):
    """页眉页脚"""
    canvas.saveState()
    canvas.setFont(_CN_FONT, 8)
    canvas.setFillColor(HexColor("#999999"))
    canvas.drawString(30, A4[1] - 20, "MathAgent 错题条")
    canvas.drawRightString(A4[0] - 30, A4[1] - 20, f"打印日期: {date.today()}")
    canvas.drawCentredString(A4[0] / 2, 15, f"第 {doc.page} 页")
    # 顶部细线
    canvas.setStrokeColor(HexColor("#cccccc"))
    canvas.line(30, A4[1] - 25, A4[0] - 30, A4[1] - 25)
    canvas.restoreState()


# ── 公共导出接口 ──

def export_error_sheet_by_student(
    db: Session,
    teacher_id: int,
    exam_name: str,
    class_name: Optional[str] = None,
    *, education_stage: str | None = None,
) -> io.BytesIO | dict:
    """按学生生成错题条PDF — 每人一张，含该生所有错题"""
    if not HAS_REPORTLAB:
        return {"error": "reportlab未安装，请执行 pip install reportlab"}

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
    results = filter_export_stage(q, education_stage).order_by(Student.class_name, Student.name).all()
    conflict = export_scope_conflict(results)
    if conflict:
        return conflict

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    student_errors = _collect_errors(results)
    if not student_errors:
        return {"error": f"考试 '{exam_name}' 没有错题记录，所有学生全部正确！"}

    styles = _make_styles()
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=25 * mm, bottomMargin=20 * mm,
    )

    elements = []
    # 标题
    elements.append(Paragraph(f"{_escape_xml(exam_name)} — 错题条", styles["CNTitle"]))
    elements.append(Paragraph(
        f"共 {len(student_errors)} 位同学有错题  |  按学生分页",
        styles["CNSubtitle"],
    ))
    elements.append(Spacer(1, 4 * mm))

    for i, s in enumerate(student_errors):
        card = _build_student_card(s, styles)
        if i > 0:
            elements.append(PageBreak())
        elements.extend(card)

    doc.build(elements, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    buf.seek(0)
    return buf


def export_error_sheet_by_question(
    db: Session,
    teacher_id: int,
    exam_name: str,
    class_name: Optional[str] = None,
    *, education_stage: str | None = None,
) -> io.BytesIO | dict:
    """按题号生成错题汇总PDF — 全班同一题的错误汇总，老师课堂讲评用"""
    if not HAS_REPORTLAB:
        return {"error": "reportlab未安装，请执行 pip install reportlab"}

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
    results = filter_export_stage(q, education_stage).order_by(Student.class_name, Student.name).all()
    conflict = export_scope_conflict(results)
    if conflict:
        return conflict

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    student_errors = _collect_errors(results)

    # 按题号分组: {q_index: {"question": ..., "errors": [...]}}
    question_groups: dict[object, dict] = {}
    for s in student_errors:
        for err in s["errors"]:
            qi = err["q_index"]
            if qi not in question_groups:
                question_groups[qi] = {
                    "question": "",
                    "correct_answer": "",
                    "solution_steps": "",
                    "errors": [],
                }
            group = question_groups[qi]
            group["question"] = group["question"] or err["question"]
            group["correct_answer"] = group["correct_answer"] or err["correct_answer"]
            group["solution_steps"] = group["solution_steps"] or err["solution_steps"]
            group["errors"].append({
                "student_name": s["name"],
                "student_no": s["student_no"],
                "student_answer": err["student_answer"],
                "status": err["status"],
            })

    if not question_groups:
        return {"error": f"考试 '{exam_name}' 没有错题记录"}

    styles = _make_styles()
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=25 * mm, bottomMargin=20 * mm,
    )

    elements = []
    elements.append(Paragraph(f"{_escape_xml(exam_name)} — 错题汇总", styles["CNTitle"]))
    elements.append(Paragraph(
        f"共 {len(question_groups)} 道题有错  |  按题号排列，适合课堂讲评",
        styles["CNSubtitle"],
    ))
    elements.append(Spacer(1, 4 * mm))

    for qi in sorted(question_groups, key=_question_sort_key):
        g = question_groups[qi]
        elems = _build_question_summary(qi, g["question"], g["errors"], styles)
        elements.extend(elems)
        elements.append(Spacer(1, 5 * mm))

    doc.build(elements, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    buf.seek(0)
    return buf


def _solution_steps_html(steps) -> str:
    if isinstance(steps, list):
        values = [escape_export_html(step) for step in steps if str(step or "").strip()]
        return "<br>".join(f"第{index}步：{value}" for index, value in enumerate(values, 1))
    if steps:
        return escape_export_html(steps)
    return "（暂无解题步骤）"


def _question_sort_key(question_no: object) -> tuple:
    chunks = re.split(r"(\d+)", str(question_no))
    return tuple(int(chunk) if chunk.isdigit() else chunk for chunk in chunks)


def _render_error_sheet_html(student_errors: list[dict], exam_name: str, mode: str) -> str:
    """Render a self-contained, offline-readable HTML error export."""
    question_mode = mode == "question"
    cards_html = []

    if question_mode:
        groups: dict[str, dict] = {}
        for student in student_errors:
            for error in student["errors"]:
                question_no = str(error.get("q_index") or "?")
                group = groups.setdefault(question_no, {
                    "question": "",
                    "correct_answer": "",
                    "solution_steps": "",
                    "errors": [],
                })
                # A historical first record may contain only a sparse grading
                # result.  Keep the first usable question, answer, and full
                # solution found across all students for this question.
                group["question"] = group["question"] or error.get("question") or ""
                group["correct_answer"] = (
                    group["correct_answer"] or error.get("correct_answer") or ""
                )
                group["solution_steps"] = (
                    group["solution_steps"] or error.get("solution_steps") or ""
                )
                group["errors"].append({
                    "student_name": student["name"],
                    "student_no": student["student_no"],
                    "student_answer": error.get("student_answer") or "未记录",
                    "status": error.get("status") or "wrong",
                })

        for question_no in sorted(groups, key=_question_sort_key):
            group = groups[question_no]
            rows = []
            for error in group["errors"]:
                status = "错误" if error["status"] in ("wrong", "错误") else "部分正确"
                rows.append(
                    "<tr>"
                    f"<td>{escape_export_html(error['student_name'])}</td>"
                    f"<td>{escape_export_html(error['student_no'])}</td>"
                    f"<td>{escape_export_html(error['student_answer'])}</td>"
                    f"<td>{status}</td>"
                    "</tr>"
                )
            cards_html.append(f'''
    <section class="student-card question-card">
        <h3>第{escape_export_html(question_no)}题 - 共{len(group["errors"])}人出错</h3>
        <div class="question">{escape_export_html(group["question"])}</div>
        <div class="correct">参考答案：{escape_export_html(group["correct_answer"])}</div>
        <div class="solution">解题步骤：<br>{_solution_steps_html(group["solution_steps"])}</div>
        <table>
            <thead><tr><th>学生姓名</th><th>学号</th><th>学生答案</th><th>判定</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </section>''')
    else:
        for student in student_errors:
            error_items = []
            for error in student["errors"]:
                question_no = str(error.get("q_index") or "").strip()
                question_label = f"第{question_no}题" if question_no and question_no != "0" else "错题"
                error_items.append(f'''
            <div class="error-item">
                <div class="q-label">{escape_export_html(question_label)}</div>
                <div class="question">{escape_export_html(error["question"])}</div>
                <div class="wrong">学生答案：{escape_export_html(error["student_answer"])}</div>
                <div class="correct">参考答案：{escape_export_html(error["correct_answer"])}</div>
                <div class="solution">解题步骤：<br>{_solution_steps_html(error["solution_steps"])}</div>
            </div>''')

            cards_html.append(f'''
    <section class="student-card">
        <h3>{escape_export_html(student["name"])} 的错题条</h3>
        <div class="meta">学号：{escape_export_html(student["student_no"])} | 班级：{escape_export_html(student["class_name"])} | 错误：{student["wrong_count"]}题</div>
        {"".join(error_items)}
    </section>''')

    title = f"{exam_name} - {'错题汇总' if question_mode else '错题条'}"
    summary = (
        f"共 {len(cards_html)} 道题有错 | 按题号汇总"
        if question_mode
        else f"共 {len(student_errors)} 位同学有错题 | 按学生整理"
    )
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{escape_export_html(title)}</title>
<style>
@page {{ size: A4; margin: 15mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; font-size: 13px; line-height: 1.6; color: #333; }}
h1 {{ text-align: center; color: #2c3e50; }}
.meta-info {{ text-align: center; color: #888; margin-bottom: 20px; }}
.student-card {{ page-break-inside: avoid; margin-bottom: 24px; border: 1px solid #ddd; border-radius: 8px; padding: 16px; }}
.student-card h3 {{ color: #2980b9; margin: 0 0 4px; }}
.meta {{ font-size: 12px; color: #888; margin-bottom: 12px; }}
.error-item {{ margin-bottom: 14px; padding: 10px; border-left: 4px solid #e67e22; background: #fff; }}
.q-label {{ font-weight: bold; color: #e67e22; margin-bottom: 4px; }}
.question {{ font-weight: bold; margin-bottom: 6px; overflow-wrap: anywhere; }}
.wrong {{ color: #e74c3c; margin-bottom: 4px; }}
.correct {{ color: #27ae60; margin-bottom: 4px; }}
.solution {{ font-size: 12px; background: #f0f9eb; padding: 8px; border-radius: 4px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; table-layout: fixed; }}
th, td {{ border: 1px solid #d8d8d8; padding: 6px; text-align: left; overflow-wrap: anywhere; }}
th {{ background: #6f42a5; color: #fff; }}
th:nth-child(1), th:nth-child(2), th:nth-child(4) {{ width: 18%; }}
@media print {{ .student-card {{ page-break-inside: avoid; }} }}
</style>
</head>
<body>
<h1>{escape_export_html(title)}</h1>
<div class="meta-info">{summary} | Ctrl+P 打印</div>
{"".join(cards_html)}
</body>
</html>'''


def export_error_sheet_html(
    db: Session,
    teacher_id: int,
    exam_name: str,
    class_name: Optional[str] = None,
    mode: str = "student",
    *, education_stage: str | None = None,
) -> str | dict:
    """降级方案：生成HTML错题条（当reportlab不可用时）"""
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
    results = filter_export_stage(q, education_stage).order_by(Student.class_name, Student.name).all()
    conflict = export_scope_conflict(results)
    if conflict:
        return conflict

    if not results:
        return {"error": f"未找到考试 '{exam_name}' 的成绩记录"}

    student_errors = _collect_errors(results)
    if not student_errors:
        return {"error": f"考试 '{exam_name}' 没有错题记录"}
    return _render_error_sheet_html(student_errors, exam_name, mode)
