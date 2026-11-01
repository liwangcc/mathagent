"""Editable A3 landscape, duplex mathematics answer-sheet DOCX."""
from __future__ import annotations

import hashlib
from io import BytesIO
from math import ceil
from typing import Any, Iterable

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from services.exercise_schema import MAX_EXERCISE_QUESTIONS

_PINK = "B34E65"
_PALE_PINK = "FAF1F3"
_BLACK = "222222"
_MUTED = "666666"
_PANEL_WIDTH_MM = 130.0
_CONTENT_HEIGHT_MM = 267.0
_LOGICAL_PAGE_COUNT = 6
_LAYOUT_VERSION = "adaptive-v2"
_SECTION_HEADER_HEIGHT_MM = 4.5
_STANDARD_FIRST_SOLUTION_HEIGHT_MM = 55.0
_MIN_FIRST_SOLUTION_HEIGHT_MM = 45.0
_FRONT_SOLUTION_HEIGHT_MM = 237.0
_BACK_SOLUTION_HEIGHT_MM = 229.0


def _as_dict(question: Any) -> dict[str, Any]:
    return question.model_dump(mode="json") if hasattr(question, "model_dump") else dict(question)


def _font(run, size=8.5, *, bold=False, color=_BLACK, name="宋体"):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)


def _borders(target, color=_PINK, size="8", edges=("top", "left", "bottom", "right", "insideH", "insideV")):
    pr = target._tbl.tblPr if hasattr(target, "_tbl") else target._tc.get_or_add_tcPr()
    tag = "w:tblBorders" if hasattr(target, "_tbl") else "w:tcBorders"
    node = pr.find(qn(tag))
    if node is None:
        node = OxmlElement(tag)
        pr.append(node)
    for edge in edges:
        item = node.find(qn(f"w:{edge}"))
        if item is None:
            item = OxmlElement(f"w:{edge}")
            node.append(item)
        item.set(qn("w:val"), "single")
        item.set(qn("w:sz"), size)
        item.set(qn("w:color"), color)


def _margins(cell, top=45, start=55, bottom=45, end=55):
    pr = cell._tc.get_or_add_tcPr()
    node = pr.find(qn("w:tcMar"))
    if node is None:
        node = OxmlElement("w:tcMar")
        pr.append(node)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        item = node.find(qn(f"w:{edge}"))
        if item is None:
            item = OxmlElement(f"w:{edge}")
            node.append(item)
        item.set(qn("w:w"), str(value))
        item.set(qn("w:type"), "dxa")


def _height(row, mm):
    row.height = Mm(mm)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))


def _shade(cell, fill):
    pr = cell._tc.get_or_add_tcPr()
    shd = pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _clear_cell(cell):
    cell.text = ""
    _margins(cell)


def _p(cell, text="", *, size=8, bold=False, color=_BLACK, align=WD_ALIGN_PARAGRAPH.LEFT):
    paragraph = cell.paragraphs[0] if not cell.paragraphs[0].text else cell.add_paragraph()
    paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.line_spacing = 1
    _font(paragraph.add_run(text), size, bold=bold, color=color)
    return paragraph


def _answer_block(cell, number: int, height_mm: float) -> None:
    table = cell.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _height(table.rows[0], height_mm)
    body = table.cell(0, 0)
    _clear_cell(body)
    _borders(body, _BLACK, "10", ("top", "left", "bottom", "right"))
    _p(body, f"{number}. 解答：", size=8.6, bold=True)


def _continuation(cell, height_mm, *, label="续答区"):
    table = cell.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _height(table.rows[0], height_mm)
    body = table.cell(0, 0)
    _clear_cell(body)
    _borders(body, _BLACK, "10", ("top", "left", "bottom", "right"))
    _p(body, f"{label}    题号：________", size=8.6, bold=True)
    hint = "请先填写续答题号，再继续作答。" if "续答" in label else "如需使用，请先填写题号再作答。"
    _p(body, hint, size=6.4, color=_PINK)


def _identity(cell, title, group_code, total_questions, *, teaching_context=None):
    from services.teaching_context import stage_label

    school_label = stage_label(teaching_context)
    if school_label == "高中":
        school_label = "普通高中"
    _p(cell, f"{school_label}数学答题卡", size=7.2, bold=True, color=_PINK, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(cell, title, size=13, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    info = cell.add_table(rows=4, cols=2)
    info.autofit = False
    _borders(info, _PINK, "7")
    labels = [
        ("班级：________________", "姓名：________________"),
        ("学号：________________", "座位号：________"),
        ("准考证号/考生号：________________________", "贴条形码区"),
        (f"本卷共 {total_questions} 题", f"模板码：{group_code}"),
    ]
    heights = (7.5, 7.5, 13, 7)
    for row, values, mm in zip(info.rows, labels, heights):
        _height(row, mm)
        for current, value in zip(row.cells, values):
            _margins(current, top=20, start=35, bottom=20, end=35)
            _p(current, value, size=6.8, bold="本卷" in value, align=WD_ALIGN_PARAGRAPH.CENTER)


def _exam_notice(cell):
    table = cell.add_table(rows=1, cols=3)
    table.autofit = False
    _borders(table, _PINK, "7")
    _height(table.rows[0], 34)
    left, middle, right = table.rows[0].cells
    for current in (left, middle, right):
        _margins(current, top=35, start=40, bottom=35, end=40)
    _shade(middle, _PALE_PINK)
    _p(left, "缺考标记", size=7, bold=True, color=_PINK, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(left, "□", size=15, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(left, "由监考教师填涂", size=5.8, color=_MUTED, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(middle, "注意事项", size=7, bold=True, color=_PINK, align=WD_ALIGN_PARAGRAPH.CENTER)
    notices = (
        "1. 填写姓名、学号并核对模板码；",
        "2. 选择题用2B铅笔规范填涂；",
        "3. 非选择题用黑色签字笔作答；",
        "4. 答案不得超出黑色矩形边框；",
        "5. 保持卡面清洁，不折叠、不污损。",
    )
    for notice in notices:
        _p(middle, notice, size=5.5)
    _p(right, "填涂示例", size=7, bold=True, color=_PINK, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(right, "正确  ■", size=6.4, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    _p(right, "错误  ☒  ✓", size=6.4, align=WD_ALIGN_PARAGRAPH.CENTER)


def _choice_layout(count: int) -> tuple[int, int, float]:
    if count <= 0:
        return 0, 0, 0.0
    cols = 3 if count <= 12 else 4
    rows = ceil(count / cols)
    return cols, rows, 7.2 if rows <= 5 else 6.3


def _fill_row_height(count: int) -> float:
    return max(5.7, min(18.0, 72 / count)) if count > 0 else 0.0


def _objective_height(choice_count: int, fill_count: int) -> float:
    _cols, choice_rows, choice_row_height = _choice_layout(choice_count)
    choice_height = (_SECTION_HEADER_HEIGHT_MM + choice_rows * choice_row_height) if choice_count else 0.0
    fill_height = (_SECTION_HEADER_HEIGHT_MM + fill_count * _fill_row_height(fill_count)) if fill_count else 0.0
    return choice_height + fill_height


def _first_solution_height(choice_count: int, fill_count: int) -> float:
    standard_objective_height = _objective_height(12, 4)
    actual_objective_height = _objective_height(choice_count, fill_count)
    return max(20.0, _STANDARD_FIRST_SOLUTION_HEIGHT_MM + standard_objective_height - actual_objective_height)


def _choice(cell, numbers):
    numbers = list(numbers)
    if not numbers:
        return
    _p(cell, "一、选择题（请用2B铅笔规范填涂）", size=8, bold=True, color=_PINK)
    cols, rows, row_height = _choice_layout(len(numbers))
    table = cell.add_table(rows=rows, cols=cols)
    table.autofit = False
    _borders(table, _PINK, "5")
    cells = [current for row in table.rows for current in row.cells]
    for row in table.rows:
        _height(row, row_height)
    for index, current in enumerate(cells):
        _margins(current, top=10, start=20, bottom=10, end=20)
        value = f"{numbers[index]}. [A] [B] [C] [D]" if index < len(numbers) else ""
        _p(current, value, size=6.6 if cols == 4 else 7, bold=bool(value))


def _fill(cell, numbers):
    numbers = list(numbers)
    if not numbers:
        return
    _p(cell, "二、填空题（请填写最终结果）", size=8, bold=True, color=_PINK)
    table = cell.add_table(rows=len(numbers), cols=1)
    _borders(table, _PINK, "5")
    row_height = _fill_row_height(len(numbers))
    for row, number in zip(table.rows, numbers):
        _height(row, row_height)
        _margins(row.cells[0], top=10, start=35, bottom=10, end=35)
        _p(row.cells[0], f"{number}.", size=7.2, bold=True)
        line = _p(row.cells[0], "____________________________________________", size=7.2)
        line.paragraph_format.left_indent = Mm(6)
        line.paragraph_format.space_before = Mm(max(0, row_height - 7.5))


def _locator_row(doc):
    table = doc.add_table(rows=1, cols=13)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for index, cell in enumerate(table.rows[0].cells):
        _margins(cell, 0, 0, 0, 0)
        if index % 2 == 0:
            _shade(cell, _BLACK)
            _p(cell, "", size=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    _height(table.rows[0], 2.5)


def _footer(cell, logical_page, group_code, side):
    _p(
        cell,
        f"数学答题卡  第{logical_page}页（共{_LOGICAL_PAGE_COUNT}页）  ·  {side}  ·  {group_code}",
        size=6.2,
        color=_PINK,
        align=WD_ALIGN_PARAGRAPH.CENTER,
    )


def _panel_container(doc):
    table = doc.add_table(rows=1, cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _borders(table, "FFFFFF", "0")
    _height(table.rows[0], _CONTENT_HEIGHT_MM)
    panels = []
    for cell in table.rows[0].cells:
        cell.width = Mm(_PANEL_WIDTH_MM)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        _clear_cell(cell)
        _margins(cell, 0, 0, 0, 0)
        _borders(cell, _PINK, "8", ("top", "left", "bottom", "right"))
        grid = cell.add_table(rows=2, cols=1)
        grid.autofit = False
        _borders(grid, "FFFFFF", "0")
        _height(grid.rows[0], 251)
        _height(grid.rows[1], 10)
        body = grid.cell(0, 0)
        footer = grid.cell(1, 0)
        _clear_cell(body)
        _clear_cell(footer)
        body.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        footer.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        panels.append((body, footer))
    return panels


def _configure(doc: DocumentObject, title):
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Mm(420)
    section.page_height = Mm(297)
    section.top_margin = Mm(7)
    section.bottom_margin = Mm(7)
    section.left_margin = Mm(8)
    section.right_margin = Mm(8)
    section.header_distance = Mm(3)
    section.footer_distance = Mm(3)
    doc.core_properties.title = title
    doc.core_properties.subject = "A3横向正反面数学答题卡"
    doc.core_properties.author = "MathAgent"
    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(8)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.space_after = Pt(0)


def _solution_panel(cell, entry, *, continuation_label="续答区", back=False):
    _p(
        cell,
        "请在对应题号的黑色矩形区域内作答，超出边框的答案无效",
        size=6.2,
        color=_PINK,
        align=WD_ALIGN_PARAGRAPH.CENTER,
    )
    available = _FRONT_SOLUTION_HEIGHT_MM if not back else _BACK_SOLUTION_HEIGHT_MM
    if entry is None:
        _continuation(cell, available, label=continuation_label)
        return
    number, _question = entry
    _answer_block(cell, number, available)


def _middle_solution_panel(cell, first_entry, second_entry, *, continuation_height=79.0):
    _p(
        cell,
        "请在对应题号的黑色矩形区域内作答，超出边框的答案无效",
        size=6.2,
        color=_PINK,
        align=WD_ALIGN_PARAGRAPH.CENTER,
    )
    if first_entry is None:
        _continuation(cell, _FRONT_SOLUTION_HEIGHT_MM, label="解答题备用区")
        return
    if second_entry is None:
        _continuation(cell, _FRONT_SOLUTION_HEIGHT_MM, label=f"{first_entry[0]}题续答区")
        return
    continuation_height = max(48.0, min(110.0, continuation_height))
    _continuation(cell, continuation_height, label=f"{first_entry[0]}题续答区")
    _answer_block(cell, second_entry[0], _FRONT_SOLUTION_HEIGHT_MM - continuation_height)


def _front(doc, title, group_code, total_questions, choice_numbers, fill_numbers, solution_entries, *, teaching_context=None):
    _locator_row(doc)
    (left, left_footer), (middle, middle_footer), (right, right_footer) = _panel_container(doc)
    _identity(left, title, group_code, total_questions, teaching_context=teaching_context)
    _exam_notice(left)
    _choice(left, choice_numbers)
    _fill(left, fill_numbers)
    first_height = _first_solution_height(len(choice_numbers), len(fill_numbers))
    first_started = bool(solution_entries) and first_height >= _MIN_FIRST_SOLUTION_HEIGHT_MM
    consumed = 0
    if first_started:
        _answer_block(left, solution_entries[0][0], first_height)
        consumed = 1
    else:
        _continuation(left, first_height, label="解答题备用区")
    _footer(left_footer, 1, group_code, "正面")

    if first_started:
        first_entry = solution_entries[0]
        second_entry = solution_entries[1] if len(solution_entries) > 1 else None
        continuation_height = 134.0 - first_height
        _middle_solution_panel(
            middle,
            first_entry,
            second_entry,
            continuation_height=continuation_height,
        )
        if second_entry is not None:
            consumed += 1
    else:
        first_entry = solution_entries[0] if solution_entries else None
        _solution_panel(middle, first_entry, continuation_label="解答题备用区")
        if first_entry is not None:
            consumed += 1
    _footer(middle_footer, 2, group_code, "正面")
    next_entry = solution_entries[consumed] if len(solution_entries) > consumed else None
    _solution_panel(right, next_entry, continuation_label="解答题备用区")
    if next_entry is not None:
        consumed += 1
    _footer(right_footer, 3, group_code, "正面")
    _locator_row(doc)
    return consumed


def _back(doc, group_code, solution_entries):
    _locator_row(doc)
    (left, left_footer), (middle, middle_footer), (right, right_footer) = _panel_container(doc)
    back_identity = (
        "姓名：____________  学号：____________  准考证号：________________  "
        f"模板码：{group_code}"
    )
    for panel in (left, middle, right):
        _p(panel, back_identity, size=6.2, bold=True, color=_PINK, align=WD_ALIGN_PARAGRAPH.CENTER)

    first_entry = solution_entries[0] if solution_entries else None
    second_entry = solution_entries[1] if len(solution_entries) > 1 else None
    third_entry = solution_entries[2] if len(solution_entries) > 2 else None
    _solution_panel(left, first_entry, continuation_label="解答题备用区", back=True)
    _footer(left_footer, 4, group_code, "背面")
    _solution_panel(middle, second_entry, continuation_label="解答题备用区", back=True)
    _footer(middle_footer, 5, group_code, "背面")
    _solution_panel(right, third_entry, continuation_label="解答题备用区", back=True)
    _footer(right_footer, 6, group_code, "背面")
    _locator_row(doc)


def build_answer_sheet_document(title: str, questions: Iterable[Any], *, teaching_context=None) -> bytes:
    """Build an editable two-side A3 landscape answer sheet for up to 22 questions."""
    from services.teaching_context import stage_label, validate_teaching_context

    snapshot = validate_teaching_context(teaching_context)
    normalized = [_as_dict(q) for q in questions]
    if not normalized:
        raise ValueError("至少需要一道题才能生成答题卡")
    if len(normalized) > MAX_EXERCISE_QUESTIONS:
        raise ValueError(f"答题卡最多支持 {MAX_EXERCISE_QUESTIONS} 道题")
    clean_title = str(title or "").strip() or f"{stage_label(snapshot)}数学答题卡"
    signature = "|".join(f"{q.get('type')}:{i}" for i, q in enumerate(normalized, 1))
    group_material = f"{_LAYOUT_VERSION}|{clean_title}|{signature}"
    group_code = "AS-" + hashlib.sha256(group_material.encode("utf-8")).hexdigest()[:10].upper()
    choice_numbers = [i for i, q in enumerate(normalized, 1) if q.get("type") == "choice"]
    fill_numbers = [i for i, q in enumerate(normalized, 1) if q.get("type") == "fill"]
    solution_entries = [(i, q) for i, q in enumerate(normalized, 1) if q.get("type") == "solution"]
    if len(solution_entries) > 6:
        raise ValueError("答题卡最多支持 6 道解答题")

    doc = Document()
    _configure(doc, clean_title)
    consumed_solutions = _front(
        doc,
        clean_title,
        group_code,
        len(normalized),
        choice_numbers,
        fill_numbers,
        solution_entries,
        teaching_context=snapshot,
    )
    doc.add_page_break()
    _back(doc, group_code, solution_entries[consumed_solutions:])
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()
