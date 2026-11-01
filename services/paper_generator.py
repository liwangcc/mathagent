"""试卷/练习卷生成服务"""

import io
import logging
from typing import Optional

from sqlalchemy.orm import Session

from services.practice import select_questions
from services.export_text import normalize_export_text

logger = logging.getLogger(__name__)

def generate_practice_paper(
    db: Session,
    topic: str,
    count: int = 5,
    chapter: Optional[str] = None,
    subject: Optional[str] = None,
    with_answers: bool = False,
    teacher_id: Optional[int] = None,
) -> dict:
    """Generate a practice worksheet for a given topic."""
    questions = select_questions(
        db, chapter=chapter, topic=topic, limit=count, subject=subject,
        teacher_id=teacher_id,
    )
    return {
        "title": f"随堂练习 — {topic}",
        "total": len(questions),
        "questions": questions,
    }


def export_paper_to_word(paper: dict, with_answers: bool = False) -> io.BytesIO:
    """Export a generated paper to Word document.

    每道题放入独立的1x1表格单元格（模拟文本框），方便老师在Word中拖拽排版。

    Args:
        paper: 包含 title, total, questions 的字典
        with_answers: 是否包含答案页（答案单独一页）
    """
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx未安装")

    from docx.shared import Pt, RGBColor

    doc = Document()

    # --- 标题 ---
    title_para = doc.add_heading(normalize_export_text(paper["title"]), level=1)
    info_para = doc.add_paragraph(f"共 {paper['total']} 题")
    info_para.runs[0].font.size = Pt(11)
    info_para.runs[0].font.color.rgb = RGBColor(100, 100, 100)

    # 分隔行
    doc.add_paragraph("")

    # --- 题目区域：每道题一个1x1表格（模拟文本框） ---
    for i, q in enumerate(paper["questions"], 1):
        question_text = normalize_export_text(q.get("question", ""))
        score = q.get("score")  # 分值（如果有的话）

        # 创建 1x1 表格
        table = doc.add_table(rows=1, cols=1)
        table.style = "Table Grid"

        cell = table.rows[0].cells[0]
        cell.width = Pt(480)  # 接近 A4 全宽

        # 设置单元格边框（粗线框，模拟文本框效果）
        _set_cell_border(cell, top={"sz": 12, "val": "single"},
                         bottom={"sz": 12, "val": "single"},
                         start={"sz": 12, "val": "single"},
                         end={"sz": 12, "val": "single"})

        # 设置单元格内边距（增加可读性）
        _set_cell_margins(cell, top=100, bottom=100, start=150, end=150)

        # 清空默认段落，手动写入带格式的内容
        cell.text = ""

        # 题号行（加粗）
        header_para = cell.paragraphs[0]
        # 题号
        run_num = header_para.add_run(f"第 {i} 题")
        run_num.bold = True
        run_num.font.size = Pt(12)
        # 分值
        if score is not None:
            run_score = header_para.add_run(f"  （{score} 分）")
            run_score.font.size = Pt(10)
            run_score.font.color.rgb = RGBColor(150, 150, 150)

        # 题目正文
        body_para = cell.add_paragraph()
        run_body = body_para.add_run(question_text)
        run_body.font.size = Pt(11)

        # 如果不显示答案，但题目中包含选项（如A/B/C/D），保持换行
        # 题目之间留空行方便调整间距
        doc.add_paragraph("")

    # --- 答案页（可选） ---
    if with_answers:
        doc.add_page_break()
        doc.add_heading("参考答案", level=1)
        doc.add_paragraph("")

        for i, q in enumerate(paper["questions"], 1):
            answer = normalize_export_text(q.get("answer", ""))
            if not answer:
                answer = "（暂无答案）"

            # 答案也用表格包裹，风格统一
            table = doc.add_table(rows=1, cols=1)
            table.style = "Table Grid"

            cell = table.rows[0].cells[0]
            _set_cell_border(cell, top={"sz": 8, "val": "single", "color": "999999"},
                             bottom={"sz": 8, "val": "single", "color": "999999"},
                             start={"sz": 8, "val": "single", "color": "999999"},
                             end={"sz": 8, "val": "single", "color": "999999"})
            _set_cell_margins(cell, top=80, bottom=80, start=120, end=120)

            cell.text = ""

            # 答案标题
            header_para = cell.paragraphs[0]
            run_title = header_para.add_run(f"第 {i} 题 答案")
            run_title.bold = True
            run_title.font.size = Pt(11)
            run_title.font.color.rgb = RGBColor(0, 100, 200)

            # 答案正文
            body_para = cell.add_paragraph()
            run_ans = body_para.add_run(answer)
            run_ans.font.size = Pt(11)

            doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _set_cell_border(cell, **kwargs):
    """设置单元格边框样式。

    kwargs: top/bottom/start/end，每个值是 dict，含 sz/val/color 键。
    """
    from docx.oxml.ns import qn

    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()

    tcBorders = tcPr.find(qn("w:tcBorders"))
    if tcBorders is None:
        from lxml import etree
        tcBorders = etree.SubElement(tcPr, qn("w:tcBorders"))

    for edge_name, attrs in kwargs.items():
        tag_name = f"w:{edge_name}"
        edge = tcBorders.find(qn(tag_name))
        if edge is None:
            from lxml import etree
            edge = etree.SubElement(tcBorders, qn(tag_name))
        for attr_key, attr_val in attrs.items():
            edge.set(qn(f"w:{attr_key}"), str(attr_val))


def _set_cell_margins(cell, top=0, bottom=0, start=0, end=0):
    """设置单元格内边距（单位：twips，1 inch = 1440 twips）。"""
    from docx.oxml.ns import qn
    from lxml import etree

    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()

    tcMar = tcPr.find(qn("w:tcMar"))
    if tcMar is None:
        tcMar = etree.SubElement(tcPr, qn("w:tcMar"))

    for side, val in [("top", top), ("bottom", bottom), ("start", start), ("end", end)]:
        elem = tcMar.find(qn(f"w:{side}"))
        if elem is None:
            elem = etree.SubElement(tcMar, qn(f"w:{side}"))
        elem.set(qn("w:w"), str(val))
        elem.set(qn("w:type"), "dxa")
