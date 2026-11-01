"""报告生成模块 — 打印友好的 docx + matplotlib 折线图。

风格来源：用户指定的打印样式
- report  极简素雅风：班级报告 + 学生个人报告
- letter  信封信纸风：致家长一封信

数据来源：services.class_analytics（成长指标）
内容排序原则：先说好的（进步亮点）→ 中性数据 → 最后说不足。
落库：register_generated_file（uuid 物理名 + owner 归属隔离）。
"""

import io
import logging
from datetime import date

from sqlalchemy.orm import Session

from models import KnowledgeEvent, Student
from models_rate_limit import TeacherClass
import services.class_analytics as analytics
from services.export_text import normalize_export_text

logger = logging.getLogger(__name__)

try:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# ── 配色（打印模板）──
PALETTE = {
    "letter": {
        "primary": "1A237E",
        "gold": "C5A55A",
        "text": "333333",
        "paper": "FFFDF7",
        "tip_bg": "F5F5F5",
        "warn_bg": "FFF3E0",
        "border": "E0E0E0",
        "bg_head": "1A237E",
        "bg_alt": "F5F5F5",
    },
    "report": {
        "primary": "1A237E",
        "text": "333333",
        "muted": "666666",
        "border": "E0E0E0",
        "grid": "F0F0F0",
        "bg_head": "F5F5F5",
        "bg_alt": "FAFAFA",
        "accent": "1565C0",
        "warn": "333333",
    },
}

# matplotlib 中文字体（findfont 探测，缺失则默认）
_ZH_FONT = None


def _zh_font():
    global _ZH_FONT
    if _ZH_FONT is None and HAS_MPL:
        for name in ("Microsoft YaHei", "SimHei", "SimSun"):
            try:
                font_manager.findfont(name, fallback_to_default=False)
                _ZH_FONT = name
                break
            except Exception:
                continue
        if _ZH_FONT is None:
            _ZH_FONT = "sans-serif"
    return _ZH_FONT


def _shade_cell(cell, hex_color: str):
    """给表格单元格设背景色。"""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _set_cell_border(cell, color="D9E2D0", val="single", size="4"):
    """给单元格设置统一细边框，保证导出 Word 和打印都有清晰边界。"""
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = tcPr.find(qn("w:tcBorders"))
    if tcBorders is None:
        tcBorders = OxmlElement("w:tcBorders")
        tcPr.append(tcBorders)
    for edge in ("top", "left", "bottom", "right"):
        tag = f"w:{edge}"
        element = tcBorders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            tcBorders.append(element)
        element.set(qn("w:val"), val)
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), color)


def _set_cell_vertical_align(cell, align="center"):
    tcPr = cell._tc.get_or_add_tcPr()
    v_align = tcPr.find(qn("w:vAlign"))
    if v_align is None:
        v_align = OxmlElement("w:vAlign")
        tcPr.append(v_align)
    v_align.set(qn("w:val"), align)


def _set_cell_margins(cell, top=90, start=120, bottom=90, end=120):
    """设置表格单元格内边距，避免 Word 默认表格过挤。单位 dxa。"""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = tcPr.find(qn("w:tcMar"))
    if tcMar is None:
        tcMar = OxmlElement("w:tcMar")
        tcPr.append(tcMar)
    for m, w in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tcMar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tcMar.append(node)
        node.set(qn("w:w"), str(w))
        node.set(qn("w:type"), "dxa")


def _set_table_width(tbl, pct=5000):
    tbl_pr = tbl._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(pct))
    tbl_w.set(qn("w:type"), "pct")


def _set_style_font(style, font_name: str, size: int | None = None):
    style.font.name = font_name
    if size:
        style.font.size = Pt(size)
    rpr = style._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font_name)


def _set_page_background(doc, color: str):
    background = doc._element.find(qn("w:background"))
    if background is None:
        background = OxmlElement("w:background")
        doc._element.insert(0, background)
    background.set(qn("w:color"), color)


def _set_page_border(doc, color: str, val: str = "double"):
    sect_pr = doc.sections[0]._sectPr
    borders = sect_pr.find(qn("w:pgBorders"))
    if borders is None:
        borders = OxmlElement("w:pgBorders")
        sect_pr.append(borders)
    borders.set(qn("w:offsetFrom"), "page")
    for edge in ("top", "left", "bottom", "right"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), val)
        node.set(qn("w:sz"), "12")
        node.set(qn("w:space"), "18")
        node.set(qn("w:color"), color)


def _clear_page_border(doc):
    sect_pr = doc.sections[0]._sectPr
    borders = sect_pr.find(qn("w:pgBorders"))
    if borders is not None:
        sect_pr.remove(borders)


def _paragraph_rule(paragraph, color: str, edge: str = "bottom", val: str = "single"):
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    node = p_bdr.find(qn(f"w:{edge}"))
    if node is None:
        node = OxmlElement(f"w:{edge}")
        p_bdr.append(node)
    node.set(qn("w:val"), val)
    node.set(qn("w:sz"), "6")
    node.set(qn("w:space"), "6")
    node.set(qn("w:color"), color)


def _cell_bottom_border(cell, color="E0E0E0", size="4"):
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = tcPr.find(qn("w:tcBorders"))
    if tcBorders is None:
        tcBorders = OxmlElement("w:tcBorders")
        tcPr.append(tcBorders)
    for edge in ("top", "left", "right"):
        node = tcBorders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            tcBorders.append(node)
        node.set(qn("w:val"), "nil")
    bottom = tcBorders.find(qn("w:bottom"))
    if bottom is None:
        bottom = OxmlElement("w:bottom")
        tcBorders.append(bottom)
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), size)
    bottom.set(qn("w:space"), "0")
    bottom.set(qn("w:color"), color)


def _add_page_number(paragraph):
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def _prepare_report_doc(doc, palette, *, footer_label="学情报告"):
    section = doc.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)
    _clear_page_border(doc)
    _set_page_background(doc, "FFFFFF")
    _set_style_font(doc.styles["Normal"], "宋体", 11)

    footer = section.footer
    p = footer.paragraphs[0]
    _paragraph_rule(p, palette["border"], edge="top")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set_run(p.add_run(f"{date.today().isoformat()}    第 "), size=9, color=palette["muted"], font="宋体")
    _add_page_number(p)
    _set_run(p.add_run(" 页"), size=9, color=palette["muted"], font="宋体")


def _prepare_letter_doc(doc, palette):
    section = doc.sections[0]
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(2)
    section.right_margin = Cm(2)
    _set_page_background(doc, palette["paper"])
    _set_style_font(doc.styles["Normal"], "宋体", 12)

    footer = section.footer
    p = footer.paragraphs[0]
    _paragraph_rule(p, palette["gold"], edge="top")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_number(p)


def _set_run(run, *, size=None, bold=False, color=None, font="微软雅黑"):
    # Word does not interpret HTML entities or LaTeX delimiters in run text.
    if run.text:
        run.text = normalize_export_text(run.text)
    if size:
        run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    run.font.name = font
    # 中文字体需设 eastAsia
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def _set_highlight(run, color: str):
    rpr = run._element.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), color)
    rpr.append(shd)


def _report_header(doc, title: str, subtitle: str, pal):
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(title), size=20, bold=True, color=pal["primary"], font="黑体")
    if subtitle:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run(sp.add_run(subtitle), size=11, color=pal["muted"], font="宋体")
    rule = doc.add_paragraph()
    _paragraph_rule(rule, pal["border"])
    doc.add_paragraph()


def _summary_line(doc, parts, pal):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    for text, strong in parts:
        run = p.add_run(str(text))
        _set_run(run, size=12, bold=strong, color=pal["primary"] if strong else pal["text"], font="宋体")


def _report_heading(doc, text, pal, size=15):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(5)
    _set_run(p.add_run(text), size=size, bold=True, color=pal["primary"], font="黑体")
    return p


def _report_table_header_cell(cell, text, pal):
    _shade_cell(cell, pal["bg_head"])
    _set_cell_margins(cell, top=100, bottom=100)
    _cell_bottom_border(cell, pal["border"])
    _set_cell_vertical_align(cell)
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(text), size=11, bold=True, color=pal["text"], font="宋体")


def _report_table_body_cell(cell, text, pal, *, alt=False, numeric=False):
    _shade_cell(cell, pal["bg_alt"] if alt else "FFFFFF")
    _set_cell_margins(cell, top=95, bottom=95)
    _cell_bottom_border(cell, pal["border"])
    _set_cell_vertical_align(cell)
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER if numeric else WD_ALIGN_PARAGRAPH.LEFT
    _set_run(p.add_run(str(text)), size=11, color=pal["text"], font="宋体")


def _table_header_cell(cell, text, palette):
    _shade_cell(cell, palette["bg_head"])
    _set_cell_margins(cell, top=100, bottom=100)
    _set_cell_border(cell, color=palette["bg_head"])
    _set_cell_vertical_align(cell)
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(text), size=10, bold=True, color="FFFFFF")


def _table_body_cell(cell, text, palette, *, alt=False, align=WD_ALIGN_PARAGRAPH.CENTER):
    if alt:
        _shade_cell(cell, palette["bg_alt"])
    else:
        _shade_cell(cell, "FFFFFF")
    _set_cell_margins(cell)
    _set_cell_border(cell, color=palette.get("border", "D9E2D0"))
    _set_cell_vertical_align(cell)
    p = cell.paragraphs[0]
    p.alignment = align
    _set_run(p.add_run(str(text)), size=10, color="333333")


def _student_class_name(db: Session, student: Student) -> str:
    if student.class_name:
        return student.class_name
    if student.class_id:
        tc = db.query(TeacherClass).filter(TeacherClass.id == student.class_id).first()
        if tc:
            return tc.class_name
    return ""


def _line_chart_png(timeline, palette) -> io.BytesIO | None:
    """月考成绩折线图 → PNG。timeline=[{exam_name, score, date}]。无数据返回 None。"""
    if not HAS_MPL or len(timeline) < 2:
        return None
    plt.rcParams["font.sans-serif"] = [_zh_font()]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(6.2, 3.1), dpi=120)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")
    xs = [t["exam_name"] or "" for t in timeline]
    ys = [t["score"] for t in timeline]
    ax.plot(range(len(xs)), ys, marker="o", markersize=4, linewidth=1.8,
            color="#" + palette["primary"])
    for i, y in enumerate(ys):
        ax.annotate(f"{y:g}", (i, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=9)
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(xs, fontsize=9)
    ax.set_ylabel("成绩", fontsize=10)
    ax.grid(True, linestyle="-", linewidth=0.8, color="#" + palette.get("grid", "F0F0F0"))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#" + palette.get("border", "E0E0E0"))
    ax.spines["bottom"].set_color("#" + palette.get("border", "E0E0E0"))
    legend = ax.legend(["成绩"], loc="upper center", bbox_to_anchor=(0.5, -0.16), frameon=False)
    for text in legend.get_texts():
        text.set_fontsize(9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


_ALL_CORRECT_LINE = "近期已批阅的作业和测验中，{name} 同学暂未出现需要重点巩固的知识点，请继续保持。"


def letter_has_content(db: Session, student_id: int, education_stage=None) -> bool:
    """Whether a parent letter would carry any grading or exam information."""
    query = db.query(KnowledgeEvent.id).filter(
        KnowledgeEvent.student_id == student_id,
        KnowledgeEvent.analytics_included.is_(True),
    )
    if analytics.filter_education_stage(query, KnowledgeEvent, education_stage).first():
        return True
    return bool(analytics.exam_timeline(db, student_id, education_stage))


def _parent_facing(weaknesses: list[dict]) -> list[dict]:
    """Drop placeholder knowledge labels (未分类/未知…) from parent-facing text."""
    return [
        w for w in weaknesses
        if str(w.get("topic") or "").strip() not in analytics._UNKNOWN_KNOWLEDGE
    ]


def _fmt_date(v) -> str:
    """Format a date/datetime/string to YYYY-MM-DD, or '-' if empty."""
    if not v:
        return "-"
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def build_class_report(db: Session, class_id: int, teacher_id: int, *, education_stage=None) -> io.BytesIO | dict:
    """班级报告（极简素雅风）：全班概览 + 薄弱点分析。"""
    if not HAS_DOCX:
        return {"error": "python-docx 未安装"}
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id, TeacherClass.teacher_id == teacher_id,
    ).first()
    if not tc:
        return {"error": "班级不存在"}

    pal = PALETTE["report"]
    table_rows = analytics.class_table(db, class_id, education_stage)
    heatmap = analytics.class_heatmap(db, class_id, education_stage)
    n = len(table_rows)

    doc = Document()
    _prepare_report_doc(doc, pal, footer_label="班级学情报告")
    report_month = f"{date.today().year}年{date.today().month}月"
    _report_header(doc, "班级学情报告", f"{tc.class_name} · {report_month} · {analytics.report_scope_label(education_stage)}", pal)

    # Observation-mode students carry no rates; students without archived
    # grading have nothing to average, and a correction rate only means
    # something for students who have made mistakes.
    observed = [r for r in table_rows if r.get("observation_mode")]
    graded = [r for r in table_rows if not r.get("observation_mode") and r.get("has_records", True)]
    correctable = [r for r in graded if r.get("has_mistakes", True)]
    avg_corr = sum(r["correction_rate"] for r in correctable) / len(correctable) if correctable else None
    avg_wrong = sum(r["wrong_rate"] for r in graded) / len(graded) if graded else None
    top_score = max((r["latest_exam_score"] for r in table_rows if r["latest_exam_score"] is not None), default=None)
    low_score = min((r["latest_exam_score"] for r in table_rows if r["latest_exam_score"] is not None), default=None)
    head_parts = [("本班共 ", False), (n, True), (" 人，参与批改 ", False), (len(graded), True), (" 人", False)]
    if observed:
        head_parts.extend([("，观察模式 ", False), (len(observed), True), (" 人（不计入统计）", False)])
    _summary_line(doc, head_parts, pal)
    score_parts = [("平均纠错率 ", False), (_pct(avg_corr), True), ("    平均错题率 ", False), (_pct(avg_wrong), True)]
    if top_score is not None:
        score_parts.extend([("    最高 ", False), (f"{top_score:g}", True)])
    if low_score is not None:
        score_parts.extend([("    最低 ", False), (f"{low_score:g}", True)])
    _summary_line(doc, score_parts, pal)

    _report_heading(doc, "学生概览", pal)
    headers = ["学号", "姓名", "错题率", "纠错率", "薄弱点", "最近月考"]
    tbl = doc.add_table(rows=1, cols=len(headers))
    _set_table_width(tbl)
    for i, h in enumerate(headers):
        _report_table_header_cell(tbl.rows[0].cells[i], h, pal)
    for ri, r in enumerate(table_rows):
        row = tbl.add_row()
        exam = f"{r['latest_exam_score']:g}" if r["latest_exam_score"] is not None else "-"
        if r.get("observation_mode"):
            rates = ["观察中", "观察中", "-"]
        elif not r.get("has_records", True):
            rates = ["暂无批阅", "-", "-"]
        else:
            rates = [
                _pct(r["wrong_rate"]),
                _pct(r["correction_rate"]) if r.get("has_mistakes", True) else "无错题",
                str(r["weakness_count"]),
            ]
        vals = [r["student_no"] or "", r["name"], *rates, exam]
        for i, v in enumerate(vals):
            _report_table_body_cell(row.cells[i], v, pal, alt=ri % 2 == 0, numeric=i not in (0, 1))
    doc.add_paragraph()

    _report_heading(doc, "薄弱点分析", pal)
    if not heatmap:
        doc.add_paragraph("暂无突出的共性薄弱点，全班掌握情况良好。")
    for ch in heatmap:
        for tp in ch["topics"]:
            p = doc.add_paragraph()
            _set_run(p.add_run("▸ "), size=11, color=pal["primary"], font="宋体")
            topic = tp["topic"] if tp["topic"] not in analytics._UNKNOWN_KNOWLEDGE else "待分类知识点"
            _set_run(p.add_run(topic), size=11, color=pal["text"], font="宋体")
            count = p.add_run(f"（{tp['wrong_students']} 人未纠正）")
            _set_run(count, size=11, bold=True, color=pal["text"], font="宋体")

    note = doc.add_paragraph()
    _paragraph_rule(note, pal["border"], edge="top")
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(note.add_run("本报告按最近批改记录生成，建议结合课堂观察使用。"), size=9, color=pal["muted"], font="宋体")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _insert_chart(doc, timeline, pal):
    if len({analytics._stage_identity(row) for row in timeline}) > 1:
        doc.add_paragraph("以下为不同学段的历史成绩，不跨学段比较升降：")
        for row in timeline:
            doc.add_paragraph(f"{analytics.report_scope_label(row.get('education_stage') or '')} · {row['exam_name']}：{row['score']:g} 分")
        return
    png = _line_chart_png(timeline, pal)
    if png:
        doc.add_picture(png, width=Inches(5.5))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif len(timeline) == 1:
        doc.add_paragraph(f"最近月考：{timeline[0]['exam_name']} {timeline[0]['score']:g} 分")
    else:
        doc.add_paragraph("暂无月考成绩记录。")


def build_student_report(db: Session, student_id: int, teacher_id: int, *, education_stage=None) -> io.BytesIO | dict:
    """学生个人报告（极简素雅风）：成绩趋势 + 薄弱点 + 纠错进展。"""
    if not HAS_DOCX:
        return {"error": "python-docx 未安装"}
    s = db.query(Student).filter(Student.id == student_id, Student.teacher_id == teacher_id).first()
    if not s:
        return {"error": "学生不存在"}

    pal = PALETTE["report"]
    detail = analytics.student_detail(db, student_id, education_stage)
    weaknesses = detail["weaknesses"]
    corrected = [w for w in weaknesses if w["status"] == "corrected"]
    pending = [w for w in weaknesses if w["status"] == "pending"]

    doc = Document()
    _prepare_report_doc(doc, pal, footer_label="学生学情报告")
    _report_header(doc, "学生学情报告", f"{s.name} · {_student_class_name(db, s) or '未分班'} · {analytics.report_scope_label(education_stage)}", pal)

    _summary_line(doc, [
        ("纠错率 ", False), (_pct(detail["correction_rate"]), True),
        ("    已攻克知识点 ", False), (len(corrected), True),
        ("    待提升知识点 ", False), (len(pending), True),
    ], pal)

    _report_heading(doc, "成绩趋势", pal)
    _insert_chart(doc, detail["exam_scores"], pal)
    doc.add_paragraph()

    if corrected:
        _report_heading(doc, "进步亮点", pal)
        rows = corrected[:8]
        tbl = doc.add_table(rows=1, cols=4)
        _set_table_width(tbl)
        for i, h in enumerate(["章节", "知识点", "状态", "首次出现"]):
            _report_table_header_cell(tbl.rows[0].cells[i], h, pal)
        for ri, w in enumerate(rows):
            row = tbl.add_row()
            _report_table_body_cell(row.cells[0], w["chapter"] or "未分类", pal, alt=ri % 2 == 0)
            _report_table_body_cell(row.cells[1], w["topic"], pal, alt=ri % 2 == 0)
            _report_table_body_cell(row.cells[2], "已攻克", pal, alt=ri % 2 == 0, numeric=True)
            _report_table_body_cell(row.cells[3], _fmt_date(w.get("first_seen")), pal, alt=ri % 2 == 0, numeric=True)

    _report_heading(doc, "薄弱点分析", pal)
    if not pending:
        doc.add_paragraph("当前没有未解决的薄弱点，继续保持！")
    else:
        tbl = doc.add_table(rows=1, cols=4)
        _set_table_width(tbl)
        for i, h in enumerate(["章节", "知识点", "出现次数", "最近出现"]):
            _report_table_header_cell(tbl.rows[0].cells[i], h, pal)
        for ri, w in enumerate(pending[:10]):
            row = tbl.add_row()
            _report_table_body_cell(row.cells[0], w["chapter"] or "未分类", pal, alt=ri % 2 == 0)
            _report_table_body_cell(row.cells[1], w["topic"], pal, alt=ri % 2 == 0)
            _report_table_body_cell(row.cells[2], w["occurrences"], pal, alt=ri % 2 == 0, numeric=True)
            _report_table_body_cell(row.cells[3], _fmt_date(w.get("last_seen")), pal, alt=ri % 2 == 0, numeric=True)

    p = doc.add_paragraph()
    _paragraph_rule(p, pal["border"], edge="top")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run("继续记录每一次订正与进步。"), size=9, color=pal["muted"], font="宋体")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def build_parent_letter(db: Session, student_id: int, teacher_id: int,
                        teacher_name: str = "", school: str = "", *, education_stage=None) -> io.BytesIO | dict:
    """致家长一封信（信封信纸风）：正式信函，含纠错进展。

    teacher_name/school 用于封面与落款署名（楷体签名）；缺省回落到占位文案。
    """
    if not HAS_DOCX:
        return {"error": "python-docx 未安装"}
    s = db.query(Student).filter(Student.id == student_id, Student.teacher_id == teacher_id).first()
    if not s:
        return {"error": "学生不存在"}

    pal = PALETTE["letter"]
    detail = analytics.student_detail(db, student_id, education_stage)
    weaknesses = _parent_facing(detail["weaknesses"])
    corrected = [w for w in weaknesses if w["status"] == "corrected"]
    pending = [w for w in weaknesses if w["status"] == "pending"]
    sign_name = (teacher_name or "").strip() or "任课教师"
    school_line = (school or "").strip()
    class_name = _student_class_name(db, s)
    today_cn = date.today().strftime("%Y 年 %m 月 %d 日")

    doc = Document()
    _prepare_letter_doc(doc, pal)
    _set_page_border(doc, pal["gold"], val="single")

    doc.add_paragraph()
    p_school = doc.add_paragraph()
    p_school.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p_school.add_run(school_line or "学校名称"), size=14, bold=True, color="000000", font="黑体")

    doc.add_paragraph()
    top_rule = doc.add_paragraph()
    _paragraph_rule(top_rule, pal["gold"], edge="top", val="double")

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(20)
    title.paragraph_format.space_after = Pt(20)
    _set_run(title.add_run("致 家 长 的 一 封 信"), size=24, bold=True, color=pal["primary"], font="黑体")

    bottom_rule = doc.add_paragraph()
    _paragraph_rule(bottom_rule, pal["gold"], edge="top", val="double")
    doc.add_paragraph()

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.LEFT
    meta.paragraph_format.left_indent = Cm(2.2)
    _set_run(meta.add_run(f"学生姓名：{s.name}同学"), size=12, color=pal["text"], font="宋体")

    cls_p = doc.add_paragraph()
    cls_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    cls_p.paragraph_format.left_indent = Cm(2.2)
    _set_run(cls_p.add_run(f"班    级：{class_name or '未分班'} · {analytics.report_scope_label(education_stage)}"), size=12, color=pal["text"], font="宋体")

    date_p = doc.add_paragraph()
    date_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    date_p.paragraph_format.left_indent = Cm(2.2)
    _set_run(date_p.add_run(f"日    期：{today_cn}"), size=12, color=pal["text"], font="宋体")

    doc.add_paragraph()
    tip_tbl = doc.add_table(rows=1, cols=1)
    tip_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_width(tip_tbl, pct=3600)
    tip_cell = tip_tbl.rows[0].cells[0]
    _shade_cell(tip_cell, pal["tip_bg"])
    _set_cell_border(tip_cell, color=pal["border"], size="4")
    _set_cell_margins(tip_cell, top=120, bottom=120)
    tip = tip_cell.paragraphs[0]
    tip.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(tip.add_run("为保护隐私，请输入手机号后4位查看"), size=12, color=pal["text"], font="宋体")
    doc.add_page_break()

    p_rule = doc.add_paragraph()
    _paragraph_rule(p_rule, pal["gold"], edge="top")

    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(head.add_run("致家长的一封信"), size=16, bold=True, color=pal["primary"], font="黑体")

    def _para(text, size=11, indent=True):
        p = doc.add_paragraph()
        if indent:
            p.paragraph_format.first_line_indent = Pt(24)
        p.paragraph_format.line_spacing = 1.5
        p.paragraph_format.space_after = Pt(6)
        _set_run(p.add_run(text), size=size, color=pal["text"], font="宋体")
        return p

    _para(f"尊敬的家长：", indent=False)
    if corrected:
        topics = "、".join(w["topic"] for w in corrected[:5])
        p = _para("", indent=True)
        _set_run(p.add_run(f"近期 {s.name} 同学在学习上有可喜的进步，已经攻克了 {topics} 等知识点，当前纠错率达到 "),
                 size=12, color=pal["text"], font="宋体")
        r = p.add_run(_pct(detail["correction_rate"]))
        _set_run(r, size=12, bold=True, color=pal["text"], font="宋体")
        _set_run(p.add_run("，学习态度值得肯定。"), size=12, color=pal["text"], font="宋体")
    else:
        _para(f"感谢您一直以来对 {s.name} 同学学习的关心与支持。", size=12)
        if detail.get("has_records") and not pending:
            _para(_ALL_CORRECT_LINE.format(name=s.name), size=12)

    if detail["exam_scores"]:
        last = detail["exam_scores"][-1]
        _para(f"在最近的 {last['exam_name']} 中，孩子取得了 {last['score']:g} 分。", size=12)

    if pending:
        topics = "、".join(w["topic"] for w in pending[:5])
        p = _para("", indent=True)
        _set_run(p.add_run("接下来若能在 "), size=12, color=pal["text"], font="宋体")
        r = p.add_run(topics)
        _set_run(r, size=12, bold=True, color=pal["text"], font="宋体")
        _set_highlight(r, pal["warn_bg"])
        _set_run(p.add_run(" 等方面多加练习，相信会有更大提升。也希望家长在家中给予适当的鼓励与督促。"),
                 size=12, color=pal["text"], font="宋体")

        tbl = doc.add_table(rows=1, cols=3)
        _set_table_width(tbl)
        for i, h in enumerate(["需关注知识点", "当前状态", "建议"]):
            _table_header_cell(tbl.rows[0].cells[i], h, pal)
        for ri, w in enumerate(pending[:5]):
            row = tbl.add_row()
            _table_body_cell(row.cells[0], w["topic"], pal, alt=ri % 2 == 1)
            _table_body_cell(row.cells[1], "待巩固", pal, alt=ri % 2 == 1)
            _table_body_cell(row.cells[2], "先补基础题，再做同类变式", pal, alt=ri % 2 == 1)

    _para("如有任何疑问，欢迎随时与我联系。顺祝", size=12, indent=True)
    _para("学业进步，阖家安康！", size=12, indent=False)

    doc.add_paragraph()
    p_rule_bottom = doc.add_paragraph()
    _paragraph_rule(p_rule_bottom, pal["gold"], edge="top")
    if school_line:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_run(sp.add_run(school_line), size=10, color="666666", font="宋体")
    sign = doc.add_paragraph()
    sign.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set_run(sign.add_run(sign_name), size=14, bold=False, color=pal["text"], font="楷体")
    dp = doc.add_paragraph()
    dp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set_run(dp.add_run(today_cn), size=10, color="666666", font="宋体")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _letter_body_lines(db: Session, s, education_stage=None) -> tuple[list[str], list[dict]]:
    """致家长信正文段落 + 需关注知识点表（docx 与 HTML 共用文案口径）。

    返回 (paragraphs, pending_rows)。遵循"先扬→中性→不足"。
    """
    detail = analytics.student_detail(db, s.id, education_stage)
    weaknesses = _parent_facing(detail["weaknesses"])
    corrected = [w for w in weaknesses if w["status"] == "corrected"]
    pending = [w for w in weaknesses if w["status"] == "pending"]

    paras = ["尊敬的家长："]
    if corrected:
        topics = "、".join(w["topic"] for w in corrected[:5])
        paras.append(f"近期 {s.name} 同学在学习上有可喜的进步，已经攻克了 {topics} 等知识点，"
                     f"当前纠错率达到 {_pct(detail['correction_rate'])}，学习态度值得肯定。")
    else:
        paras.append(f"感谢您一直以来对 {s.name} 同学学习的关心与支持。")
        if detail.get("has_records") and not pending:
            paras.append(_ALL_CORRECT_LINE.format(name=s.name))
    if detail["exam_scores"]:
        last = detail["exam_scores"][-1]
        paras.append(f"在最近的 {last['exam_name']} 中，孩子取得了 {last['score']:g} 分。")
    if pending:
        topics = "、".join(w["topic"] for w in pending[:5])
        paras.append(f"接下来若能在 {topics} 等方面多加练习，相信会有更大提升。"
                     f"也希望家长在家中给予适当的鼓励与督促。")
    paras.append("如有任何疑问，欢迎随时与我联系。顺祝")
    paras.append("学业进步，阖家安康！")
    rows = [{"topic": w["topic"], "perf": "待巩固"} for w in pending[:5]]
    return paras, rows


def render_parent_letter_html(db: Session, student_id: int, teacher_id: int,
                              teacher_name: str = "", school: str = "", *, education_stage=None) -> str | dict:
    """致家长信的移动端 HTML（信纸风）。供群发链接校验通过后展示。

    与 build_parent_letter 共用 _letter_body_lines 文案，楷体署名。
    返回完整 HTML 字符串；学生不存在返回 {"error": ...}。
    """
    from html import escape
    s = db.query(Student).filter(Student.id == student_id, Student.teacher_id == teacher_id).first()
    if not s:
        return {"error": "学生不存在"}

    pal = PALETTE["letter"]
    primary = "#" + pal["primary"]
    gold = "#" + pal["gold"]
    paper = "#" + pal["paper"]
    border = "#" + pal["border"]
    warn_bg = "#" + pal["warn_bg"]
    paras, rows = _letter_body_lines(db, s, education_stage)
    sign_name = escape((teacher_name or "").strip() or "任课教师")
    school_line = escape((school or "").strip())
    class_name = escape(_student_class_name(db, s) or "未分班")
    today = date.today().strftime("%Y 年 %m 月 %d 日")

    body_parts = []
    focus_text = "、".join(r["topic"] for r in rows[:5]) if rows else ""
    for t in paras:
        if focus_text and focus_text in t and t.startswith("接下来若能在 "):
            before, after = t.split(focus_text, 1)
            body_parts.append(
                f'<p class="body">{escape(before)}<span class="focus">{escape(focus_text)}</span>{escape(after)}</p>'
            )
        else:
            body_parts.append(f'<p class="body">{escape(t)}</p>')
    body_html = "".join(body_parts)
    table_html = ""
    if rows:
        trs = "".join(
            f"<tr><td>{escape(r['topic'])}</td><td>{escape(r['perf'])}</td>"
            f"<td>先补基础题，再做同类变式</td></tr>" for r in rows
        )
        table_html = (
            '<table class="weak"><thead><tr><th>需关注知识点</th><th>当前状态</th>'
            f'<th>建议</th></tr></thead><tbody>{trs}</tbody></table>'
        )
    school_top = f'<div class="school">{school_line}</div>' if school_line else ""
    school_sign = f'<div class="sign-school">{school_line}</div>' if school_line else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=2">
<title>致家长的一封信</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: {paper}; font-family: "SimSun", "Songti SC", serif;
         color: #333; line-height: 1.8; }}
  .sheet {{ max-width: 680px; margin: 0 auto; background: {paper}; min-height: 100vh;
            padding: 34px 24px 40px; border-left: 1px solid {gold}; border-right: 1px solid {gold}; }}
  .school {{ text-align: center; color: #333; font-family: "SimHei", sans-serif; font-size: 15px; }}
  h1 {{ text-align: center; color: {primary}; font-family: "SimHei", sans-serif;
        font-size: 26px; font-weight: 700; margin: 18px 0; letter-spacing: 0; }}
  .meta {{ text-align: center; color: #666; font-size: 13px; margin-bottom: 18px; }}
  .rule {{ border-top: 3px double {gold}; margin: 18px 0; }}
  .body {{ font-size: 16px; text-indent: 2em; margin: 14px 0; }}
  .body:first-of-type {{ text-indent: 0; font-weight: 600; }}
  .focus {{ font-weight: 700; background: {warn_bg}; }}
  table.weak {{ width: 100%; border-collapse: collapse; border: 1px solid {border};
                margin: 20px 0; font-size: 14px; }}
  table.weak th {{ background: {primary}; color: #fff; padding: 9px 8px; text-align: left; }}
  table.weak td {{ padding: 9px 8px; border-bottom: 1px solid {border}; }}
  table.weak tr:nth-child(even) td {{ background: #F5F5F5; }}
  .sign {{ margin-top: 36px; text-align: right; }}
  .sign-school {{ color: #666; font-size: 13px; }}
  .sign-name {{ font-family: "KaiTi", "STKaiti", "Kaiti SC", serif; color: #333;
                font-size: 24px; margin: 6px 0; }}
  .sign-date {{ color: #666; font-size: 13px; }}
  .footer-rule {{ border-top: 1px solid {gold}; margin-top: 30px; padding-top: 10px;
                  color: #777; font-size: 12px; text-align: center; }}
  @media print {{
    body {{ background: #fff; }}
    .sheet {{ box-shadow: none; max-width: none; }}
  }}
</style>
</head>
<body>
  <div class="sheet">
    {school_top}
    <div class="rule"></div>
    <h1>致 家 长 的 一 封 信</h1>
    <div class="rule"></div>
    <div class="meta">{escape(s.name)} 同学 ｜ {class_name} ｜ {escape(analytics.report_scope_label(education_stage))} ｜ {today}</div>
    {body_html}
    {table_html}
    <div class="sign">
      {school_sign}
      <div class="sign-name">{sign_name}</div>
      <div class="sign-date">{today}</div>
    </div>
    <div class="footer-rule">本信件由 MathAgent 学情报告生成，请结合课堂反馈共同使用。</div>
  </div>
</body>
</html>"""


# 报告类型 → 构建函数
STUDENT_BUILDERS = {
    "student": build_student_report,
    "parent_letter": build_parent_letter,
}
