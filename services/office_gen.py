"""识图办公 — 文档生成服务

小M 读懂模板后输出统一结构（structure dict），本模块按 format 渲染成
Word(.docx) / Excel(.xlsx) / PPT(.pptx)，返回 BytesIO 供直接下载。

structure 约定（小M 输出的 JSON）：
{
  "format": "word" | "excel" | "ppt",
  "title": "文档标题",
  "sections": [                # word/ppt 用
    {"heading": "小标题", "body": "正文段落，可含\\n换行"}
  ],
  "table": {                   # excel 用（也可 word 内嵌）
    "headers": ["列1", "列2"],
    "rows": [["a", "b"], ["c", "d"]]
  },
  "slides": [                  # ppt 用（无则用 sections 兜底）
    {"title": "页标题", "bullets": ["要点1", "要点2"]}
  ]
}
"""

import io
import logging

logger = logging.getLogger(__name__)

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    import openpyxl
    from openpyxl.styles import Font, Alignment
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    from pptx import Presentation
    HAS_PPTX = True
except ImportError:
    HAS_PPTX = False


_MEDIA = {
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_EXT = {"word": "docx", "excel": "xlsx", "ppt": "pptx"}


def _safe_title(structure: dict) -> str:
    t = (structure.get("title") or "").strip()
    return t or "文档"


def build_word(structure: dict) -> io.BytesIO:
    if not HAS_DOCX:
        raise RuntimeError("python-docx 未安装")
    doc = Document()
    doc.add_heading(_safe_title(structure), level=0)

    for sec in structure.get("sections") or []:
        heading = (sec.get("heading") or "").strip()
        if heading:
            doc.add_heading(heading, level=1)
        body = sec.get("body") or ""
        for para in str(body).split("\n"):
            doc.add_paragraph(para)

    table = structure.get("table") or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if headers or rows:
        ncols = len(headers) if headers else (len(rows[0]) if rows else 0)
        if ncols:
            t = doc.add_table(rows=0, cols=ncols)
            t.style = "Table Grid"
            if headers:
                cells = t.add_row().cells
                for i, h in enumerate(headers[:ncols]):
                    cells[i].text = str(h)
            for r in rows:
                cells = t.add_row().cells
                for i, v in enumerate(list(r)[:ncols]):
                    cells[i].text = str(v)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def build_excel(structure: dict) -> io.BytesIO:
    if not HAS_OPENPYXL:
        raise RuntimeError("openpyxl 未安装")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = _safe_title(structure)[:31] or "Sheet1"

    row_cursor = 1
    title = _safe_title(structure)
    ws.cell(row=row_cursor, column=1, value=title).font = Font(bold=True, size=14)
    row_cursor += 2

    table = structure.get("table") or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []

    if headers:
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=row_cursor, column=c, value=str(h))
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        row_cursor += 1
    for r in rows:
        for c, v in enumerate(list(r), start=1):
            ws.cell(row=row_cursor, column=c, value=str(v))
        row_cursor += 1

    # 无表格时，把 sections 退化成两列（小标题/正文）
    if not headers and not rows:
        for sec in structure.get("sections") or []:
            ws.cell(row=row_cursor, column=1, value=str(sec.get("heading") or "")).font = Font(bold=True)
            ws.cell(row=row_cursor, column=2, value=str(sec.get("body") or ""))
            row_cursor += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_ppt(structure: dict) -> io.BytesIO:
    if not HAS_PPTX:
        raise RuntimeError("python-pptx 未安装")
    prs = Presentation()

    # 封面
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = _safe_title(structure)

    # slides 优先；没有则用 sections 兜底
    slides = structure.get("slides")
    if not slides:
        slides = [
            {"title": sec.get("heading") or "", "bullets": [b for b in str(sec.get("body") or "").split("\n") if b.strip()]}
            for sec in (structure.get("sections") or [])
        ]

    for s in slides:
        layout = prs.slide_layouts[1]  # title + content
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = str(s.get("title") or "")
        body_ph = slide.placeholders[1]
        tf = body_ph.text_frame
        bullets = s.get("bullets") or []
        if bullets:
            tf.text = str(bullets[0])
            for b in bullets[1:]:
                p = tf.add_paragraph()
                p.text = str(b)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def build_office(structure: dict) -> tuple[io.BytesIO, str, str]:
    """统一入口：按 structure['format'] 分派，返回 (BytesIO, filename, media_type)。"""
    fmt = (structure.get("format") or "word").lower()
    if fmt not in ("word", "excel", "ppt"):
        fmt = "word"

    if fmt == "excel":
        buf = build_excel(structure)
    elif fmt == "ppt":
        buf = build_ppt(structure)
    else:
        buf = build_word(structure)

    filename = f"{_safe_title(structure)}.{_EXT[fmt]}"
    return buf, filename, _MEDIA[fmt]
