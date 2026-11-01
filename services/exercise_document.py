"""Editable A3 two-column Word rendering for structured exercise papers."""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Iterable

import math2docx
from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import (
    WD_CELL_VERTICAL_ALIGNMENT,
    WD_TABLE_ALIGNMENT,
)
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Mm, Pt, RGBColor
from lxml import etree

from services.export_text import decode_export_text


_TYPE_META = {
    "choice": ("一、选择题", "每小题只有一个选项符合题意"),
    "fill": ("二、填空题", "请将答案填写在题中横线上"),
    "solution": ("三、解答题", "请写出必要的计算过程或推理步骤"),
}
_TYPE_ORDER = ("choice", "fill", "solution")
_BLUE = RGBColor(36, 88, 120)
_MUTED = RGBColor(89, 99, 110)
_MATH_SEGMENT_RE = re.compile(
    r"\$\$(.+?)\$\$|\$(.+?)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]",
    re.DOTALL,
)
_CHOICE_ANSWER_SLOT_RE = re.compile(r"[（(][\s\u3000]*[）)]")
_LATEX_ARRAY_RE = re.compile(
    r"(?:\\\[|\$\$)?\s*"
    r"\\begin\{(?P<environment>array|tabular)\}\s*\{[^{}]*\}"
    r"(?P<body>.*?)"
    r"\\end\{(?P=environment)\}\s*"
    r"(?:\\\]|\$\$)?",
    re.DOTALL,
)
_MALFORMED_ARRAY_RE = re.compile(
    r"\(\s*\|[clr](?:\|[clr])+\|\s*\)\s*(?P<body>(?:\\?hline\b).*)$",
    re.DOTALL | re.IGNORECASE,
)
_MALFORMED_ARRAY_MARKER_RE = re.compile(
    r"\(\s*\|(?P<format>[clr](?:\|[clr])+)\|\s*\)",
    re.IGNORECASE,
)
_FLAT_MARKDOWN_MARKER_RE = re.compile(
    r"\|\s*(?:(?::?-+:?)\s*)?\|"
)
_MARKDOWN_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")
_PAGE_WIDTH_MM = 420.0
_PAGE_HEIGHT_MM = 297.0
_OUTER_MARGIN_MM = 20.0
_BINDING_STRIP_WIDTH_MM = 35.0
_COLUMN_GAP_TWIPS = 840
_COLUMN_GAP_MM = _COLUMN_GAP_TWIPS * 25.4 / 1440
_COLUMN_WIDTH_MM = (
    _PAGE_WIDTH_MM - _BINDING_STRIP_WIDTH_MM - _OUTER_MARGIN_MM - _COLUMN_GAP_MM
) / 2
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_V_NS = "urn:schemas-microsoft-com:vml"
_O_NS = "urn:schemas-microsoft-com:office:office"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_LATEX_SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±",
    r"\mp": "∓", r"\leqslant": "≤", r"\leq": "≤",
    r"\geqslant": "≥", r"\geq": "≥", r"\neq": "≠",
    r"\approx": "≈", r"\infty": "∞", r"\in": "∈", r"\notin": "∉",
    r"\subseteq": "⊆", r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩",
    r"\varnothing": "∅", r"\emptyset": "∅", r"\parallel": "∥",
    r"\perp": "⊥", r"\angle": "∠", r"\rightarrow": "→",
    r"\Rightarrow": "⇒", r"\therefore": "∴", r"\because": "∵",
    r"\pi": "π", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\theta": "θ", r"\lambda": "λ", r"\rho": "ρ", r"\Delta": "Δ",
    r"\sum": "Σ", r"\circ": "°", r"\degree": "°",
}


def _as_dict(question: Any) -> dict[str, Any]:
    if hasattr(question, "model_dump"):
        return question.model_dump(mode="json")
    return dict(question)


def _clean_text(value: Any) -> str:
    # Model responses may be entity-encoded more than once (for example
    # ``&amp;#x20;``). Decode a bounded number of layers before rendering.
    text = decode_export_text(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:p|div|span|strong|em|b|i)\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = text.replace("**", "")
    return text


def _clean_question_text(value: Any) -> str:
    """Remove source-list markup without deleting a cited original number."""
    text = _clean_text(value)
    text = re.sub(r"^\s*\d{1,3}\s*[.．、]\s*", "", text, count=1)
    text = re.sub(
        r"^(【[^】]{1,120}】)\s*\d{1,3}\s*[.．、]\s*",
        r"\1",
        text,
        count=1,
    )
    text = re.sub(
        r"\$?\s*\^\{?\\circ\}?\s*(?:\\mathrm\{?C\}?|C)\s*\$?",
        "℃",
        text,
    )
    text = re.sub(
        r"最高气温\s*[（(]\s*单位\s*[:：]\s*[）)]",
        "最高气温（单位：℃）",
        text,
    )
    return text.strip()


def _set_run_font(run, name: str = "宋体", size: float = 10.5, *, bold: bool = False, color=None) -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)
    if color is not None:
        run.font.color.rgb = color


def _readable_latex_fallback(value: str) -> str:
    """Make malformed LaTeX readable instead of leaking raw source commands."""
    text = value.strip().replace("$", "")
    # These fragments are produced when a Markdown/LaTeX table is flattened
    # to plain text.  They are layout syntax, not mathematical content.
    text = re.sub(r"\(\s*\|[clr](?:\|[clr])+\|\s*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*\|\s*(?:[-: ]*)\|\s*$", "", text)
    text = re.sub(r"(?<![A-Za-z])\\?hline\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", text)
    text = re.sub(
        r"\\(?:mathrm|mathbf|mathit|text|operatorname)\{([^{}]*)\}",
        r"\1",
        text,
    )
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"\\", "；")
    for source, replacement in sorted(_LATEX_SYMBOLS.items(), key=lambda item: -len(item[0])):
        text = text.replace(source, replacement)
    text = re.sub(r"\^\{([0-9+-])\}", r"^\1", text)
    text = re.sub(r"_\{([^{}]+)\}", r"_\1", text)
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"\\(?:begin|end)\([^)]*\)", "", text)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    text = re.sub(r"\s*&\s*", " ", text)
    text = re.sub(r"\\{2,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_table_cell(value: str) -> str:
    text = _clean_text(value).strip(" \t;；\\")
    text = text.removeprefix("$").removesuffix("$").strip()
    text = _readable_latex_fallback(text)
    if not text or re.fullmatch(r"[(){}\s]*", text):
        return "—"
    return text


def _parse_latex_table_body(
    body: str,
    *,
    expected_columns: int | None = None,
) -> list[list[str]]:
    """Parse both regular and lightly damaged LaTeX table bodies.

    OCR/model responses often drop one of the backslashes in ``\\hline`` or
    the row terminator.  Splitting those control tokens before processing
    cells keeps them out of the visible document while retaining the values.
    """
    normalized = body.replace("\r", "\n")
    normalized = re.sub(r"\\{2,}(?:\s*\[[^\]]*\])?", "\n", normalized)
    normalized = re.sub(r"(?<![A-Za-z])\\?hline\b", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(?<![A-Za-z])\\?cline(?:\s*\{[^}]*\})?", "\n", normalized, flags=re.IGNORECASE)
    rows: list[list[str]] = []
    for line in normalized.splitlines():
        line = line.strip(" \t;；\\")
        if "&" not in line:
            continue
        cells = [_clean_table_cell(cell) for cell in line.split("&")]
        if len(cells) >= 2 and any(cell != "—" for cell in cells):
            rows.append(cells)
    if not rows:
        return []
    column_count = max(len(row) for row in rows)
    if expected_columns and expected_columns >= 2:
        column_count = max(column_count, expected_columns)
    return [row + ["—"] * (column_count - len(row)) for row in rows]


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_clean_table_cell(cell) for cell in stripped.split("|")]


def _is_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and not _is_flat_markdown_marker(stripped)


def _is_markdown_separator(line: str) -> bool:
    if not _is_markdown_table_row(line):
        return False
    cells = line.strip().strip("|").split("|")
    return len(cells) >= 2 and all(
        _MARKDOWN_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in cells
    )


def _is_flat_markdown_marker(line: str) -> bool:
    """Return whether a renderer flattened a one-cell Markdown table row."""
    return bool(_FLAT_MARKDOWN_MARKER_RE.fullmatch(line.strip()))


def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    return lines, offsets


def _infer_flat_table_columns(cell_count: int) -> int:
    """Infer a useful column count after a Markdown renderer lost row breaks."""
    if cell_count < 2:
        return cell_count
    # Chinese exam tables most commonly have four columns; retaining this
    # preference also reconstructs the 4x4 machine-quality table correctly.
    for preferred in (4, 3, 2, 5, 6, 7, 8):
        if cell_count % preferred == 0:
            return preferred
    for candidate in range(min(12, cell_count), 1, -1):
        if cell_count % candidate == 0:
            return candidate
    return min(4, cell_count)


def _flattened_markdown_blocks(text: str) -> list[tuple[int, int, list[list[str]]]]:
    """Recover tables emitted as repeated ``|   |``/``| - |`` fragments."""
    lines, offsets = _line_offsets(text)
    blocks: list[tuple[int, int, list[list[str]]]] = []
    index = 0
    while index < len(lines):
        if not _is_flat_markdown_marker(lines[index]):
            index += 1
            continue
        start_index = index
        marker_count = 0
        values: list[str] = []
        while index < len(lines) and _is_flat_markdown_marker(lines[index]):
            marker_count += 1
            index += 1
            # Blank lines are introduced by Markdown-to-text conversion and
            # do not delimit a cell.
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines) or _is_flat_markdown_marker(lines[index]):
                continue
            values.append(lines[index].strip())
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines) or not _is_flat_markdown_marker(lines[index]):
                break

        if marker_count < 4 or len(values) < 4 or marker_count < len(values):
            # Resume after the first marker so a later valid block can still
            # be found, without swallowing ordinary prose.
            index = start_index + 1
            continue
        column_count = _infer_flat_table_columns(len(values))
        if column_count < 2 or len(values) % column_count:
            index = start_index + 1
            continue
        rows = [
            [_clean_table_cell(value) for value in values[offset : offset + column_count]]
            for offset in range(0, len(values), column_count)
        ]
        end_offset = offsets[index] if index < len(offsets) else len(text)
        blocks.append((offsets[start_index], end_offset, rows))
    return blocks


def _markdown_table_blocks(text: str) -> list[tuple[int, int, list[list[str]]]]:
    lines, offsets = _line_offsets(text)
    blocks: list[tuple[int, int, list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        if not _is_markdown_table_row(lines[index]) or not _is_markdown_separator(lines[index + 1]):
            index += 1
            continue
        start_index = index
        rows = [_split_markdown_row(lines[index])]
        index += 2
        while index < len(lines) and _is_markdown_table_row(lines[index]) and not _is_markdown_separator(lines[index]):
            rows.append(_split_markdown_row(lines[index]))
            index += 1
        if len(rows) >= 2 and max(map(len, rows)) >= 2:
            column_count = max(map(len, rows))
            rows = [row + ["—"] * (column_count - len(row)) for row in rows]
            end_offset = offsets[index] if index < len(offsets) else len(text)
            blocks.append((offsets[start_index], end_offset, rows))
    return blocks


def _malformed_latex_blocks(text: str) -> list[tuple[int, int, list[list[str]]]]:
    markers = list(_MALFORMED_ARRAY_MARKER_RE.finditer(text))
    blocks: list[tuple[int, int, list[list[str]]]] = []
    for index, marker in enumerate(markers):
        next_start = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        body = text[marker.end() : next_start]
        formats = marker.group("format").split("|")
        rows = _parse_latex_table_body(body, expected_columns=len(formats))
        if not rows:
            continue
        end = next_start
        # Do not consume prose after the final malformed table.  The last
        # hline is a reliable boundary in the damaged form emitted by models.
        hlines = list(re.finditer(r"(?<![A-Za-z])\\?hline\b", body, re.IGNORECASE))
        if hlines and index + 1 == len(markers):
            end = marker.end() + hlines[-1].end()
        blocks.append((marker.start(), end, rows))
    return blocks


def _table_spans(text: str) -> list[tuple[int, int, list[list[str]]]]:
    candidates: list[tuple[int, int, list[list[str]], int]] = []
    for match in _LATEX_ARRAY_RE.finditer(text):
        rows = _parse_latex_table_body(match.group("body"))
        if rows:
            candidates.append((match.start(), match.end(), rows, 3))
    candidates.extend((*block, 2) for block in _malformed_latex_blocks(text))
    candidates.extend((*block, 1) for block in _markdown_table_blocks(text))
    candidates.extend((*block, 0) for block in _flattened_markdown_blocks(text))

    # Prefer the more explicit parser when two candidates overlap, then keep
    # the source order for independent tables.
    candidates.sort(key=lambda item: (item[0], -item[3], -(item[1] - item[0])))
    accepted: list[tuple[int, int, list[list[str]]]] = []
    for start, end, rows, _priority in candidates:
        if end <= start or any(start < other_end and end > other_start for other_start, other_end, _ in accepted):
            continue
        accepted.append((start, end, rows))
    return sorted(accepted, key=lambda item: item[0])


def _extract_question_parts(value: Any) -> list[tuple[str, str | list[list[str]]]]:
    """Return text/table parts in source order for the Word renderer."""
    text = _clean_question_text(value)
    spans = _table_spans(text)
    if not spans:
        return [("text", text)]
    parts: list[tuple[str, str | list[list[str]]]] = []
    cursor = 0
    for start, end, rows in spans:
        before = text[cursor:start]
        if before.strip():
            parts.append(("text", before.strip()))
        parts.append(("table", rows))
        cursor = end
    trailing = text[cursor:]
    if trailing.strip():
        parts.append(("text", trailing.strip()))
    return parts


def _add_plain_run(paragraph, text: str, *, name: str, size: float, bold: bool, color) -> None:
    if not text:
        return
    if (
        "$" in text
        or re.search(r"\\[A-Za-z]+", text)
        or re.search(r"(?i)hline", text)
        or re.search(r"\|\s*[-: ]*\|", text)
    ):
        text = _readable_latex_fallback(text)
    if text:
        _set_run_font(paragraph.add_run(text), name, size, bold=bold, color=color)


def _add_rich_line(paragraph, line: str, *, name: str, size: float, bold: bool, color) -> None:
    cursor = 0
    for match in _MATH_SEGMENT_RE.finditer(line):
        _add_plain_run(
            paragraph,
            line[cursor : match.start()],
            name=name,
            size=size,
            bold=bold,
            color=color,
        )
        latex = next((group for group in match.groups() if group and group.strip()), "")
        if latex:
            try:
                math2docx.add_math(paragraph, latex.strip())
            except Exception:
                _add_plain_run(
                    paragraph,
                    _readable_latex_fallback(latex),
                    name=name,
                    size=size,
                    bold=bold,
                    color=color,
                )
        cursor = match.end()
    _add_plain_run(
        paragraph,
        line[cursor:],
        name=name,
        size=size,
        bold=bold,
        color=color,
    )


def _hide_square_root_degree_placeholders(element) -> None:
    """Hide the empty radical degree that WPS otherwise renders as a box."""
    for radical in element.iter(qn("m:rad")):
        degree = radical.find(qn("m:deg"))
        if degree is not None and any(
            str(node.text or "").strip() for node in degree.iter()
        ):
            continue
        properties = radical.find(qn("m:radPr"))
        if properties is None:
            properties = OxmlElement("m:radPr")
            radical.insert(0, properties)
        degree_hidden = properties.find(qn("m:degHide"))
        if degree_hidden is None:
            degree_hidden = OxmlElement("m:degHide")
            properties.insert(0, degree_hidden)
        degree_hidden.set(qn("m:val"), "1")


def _add_text(paragraph, text: Any, *, name="宋体", size=10.5, bold=False, color=None) -> None:
    lines = _clean_text(text).split("\n")
    for index, line in enumerate(lines):
        if index:
            paragraph.add_run().add_break()
        _add_rich_line(paragraph, line, name=name, size=size, bold=bold, color=color)


def _set_page(section, *, binding_side: str) -> None:
    if binding_side not in {"left", "right"}:
        raise ValueError(f"不支持的装订线方向：{binding_side}")
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Mm(_PAGE_WIDTH_MM)
    section.page_height = Mm(_PAGE_HEIGHT_MM)
    section.top_margin = Cm(1.35)
    section.bottom_margin = Cm(1.35)
    section.left_margin = Mm(
        _BINDING_STRIP_WIDTH_MM if binding_side == "left" else _OUTER_MARGIN_MM
    )
    section.right_margin = Mm(
        _OUTER_MARGIN_MM if binding_side == "left" else _BINDING_STRIP_WIDTH_MM
    )
    section.header_distance = Mm(12)
    section.footer_distance = Cm(0.6)
    document_grid = section._sectPr.find(qn("w:docGrid"))
    if document_grid is None:
        document_grid = OxmlElement("w:docGrid")
        section._sectPr.append(document_grid)
    document_grid.set(qn("w:type"), "linesAndChars")
    document_grid.set(qn("w:linePitch"), "312")
    document_grid.set(qn("w:charSpace"), "-3149")


def _set_columns(section, count: int, *, separator: bool = False) -> None:
    sect_pr = section._sectPr
    columns = sect_pr.find(qn("w:cols"))
    if columns is None:
        columns = OxmlElement("w:cols")
        sect_pr.append(columns)
    columns.set(qn("w:num"), str(count))
    columns.set(qn("w:equalWidth"), "1")
    columns.set(qn("w:space"), str(_COLUMN_GAP_TWIPS))
    columns.set(qn("w:sep"), "1" if separator and count > 1 else "0")


def _set_cell_margins(cell, *, top=45, start=70, bottom=45, end=70) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_table_borders(table, *, value="nil", color="FFFFFF", size="0") -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = borders.find(qn(f"w:{edge}"))
        if tag is None:
            tag = OxmlElement(f"w:{edge}")
            borders.append(tag)
        tag.set(qn("w:val"), value)
        tag.set(qn("w:sz"), size)
        tag.set(qn("w:color"), color)


def _add_two_column_question_table(doc):
    """Create a WPS-stable second-page layout with the page-one physical gutter."""
    table = doc.add_table(rows=1, cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _set_table_borders(table, value="nil")

    column_widths = (
        Mm(_COLUMN_WIDTH_MM),
        Mm(_COLUMN_GAP_MM),
        Mm(_COLUMN_WIDTH_MM),
    )
    for column, width in zip(table.columns, column_widths):
        column.width = width
    for cell, width in zip(table.rows[0].cells, column_widths):
        cell.width = width
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        _set_cell_margins(cell, top=0, bottom=0, start=0, end=0)
    left, _gutter, right = table.rows[0].cells
    return left, right


def _append_vml_textbox(
    paragraph,
    *,
    shape_id: str,
    spid: str,
    shape_style: str,
    textbox_style: str,
    paragraph_specs: tuple[
        tuple[str, tuple[tuple[str, float | None, bool], ...]], ...
    ],
) -> None:
    """Append a WPS-compatible VML textbox matching the supplied exam template."""
    pict = etree.Element(
        f"{{{_W_NS}}}pict",
        nsmap={"w": _W_NS, "v": _V_NS, "o": _O_NS},
    )
    shape = etree.SubElement(pict, f"{{{_V_NS}}}shape")
    shape.set("id", shape_id)
    shape.set(f"{{{_O_NS}}}spid", spid)
    shape.set(f"{{{_O_NS}}}spt", "202")
    shape.set("type", "#_x0000_t202")
    shape.set("style", shape_style)
    shape.set("filled", "f")
    shape.set("stroked", "f")
    shape.set("coordsize", "21600,21600")

    etree.SubElement(shape, f"{{{_V_NS}}}path")
    fill = etree.SubElement(shape, f"{{{_V_NS}}}fill")
    fill.set("on", "f")
    fill.set("focussize", "0,0")
    stroke = etree.SubElement(shape, f"{{{_V_NS}}}stroke")
    stroke.set("on", "f")
    stroke.set("joinstyle", "miter")
    image_data = etree.SubElement(shape, f"{{{_V_NS}}}imagedata")
    image_data.set(f"{{{_O_NS}}}title", "")
    lock = etree.SubElement(shape, f"{{{_O_NS}}}lock")
    lock.set(f"{{{_V_NS}}}ext", "edit")

    textbox = etree.SubElement(shape, f"{{{_V_NS}}}textbox")
    textbox.set("style", textbox_style)
    content = etree.SubElement(textbox, f"{{{_W_NS}}}txbxContent")
    for alignment, run_specs in paragraph_specs:
        inner_paragraph = etree.SubElement(content, f"{{{_W_NS}}}p")
        paragraph_properties = etree.SubElement(inner_paragraph, f"{{{_W_NS}}}pPr")
        justification = etree.SubElement(paragraph_properties, f"{{{_W_NS}}}jc")
        justification.set(f"{{{_W_NS}}}val", alignment)
        for text, size, underline in run_specs:
            run = etree.SubElement(inner_paragraph, f"{{{_W_NS}}}r")
            run_properties = etree.SubElement(run, f"{{{_W_NS}}}rPr")
            fonts = etree.SubElement(run_properties, f"{{{_W_NS}}}rFonts")
            fonts.set(f"{{{_W_NS}}}hint", "eastAsia")
            fonts.set(f"{{{_W_NS}}}ascii", "宋体")
            fonts.set(f"{{{_W_NS}}}hAnsi", "宋体")
            if size is not None:
                font_size = etree.SubElement(run_properties, f"{{{_W_NS}}}sz")
                font_size.set(f"{{{_W_NS}}}val", str(round(size * 2)))
                complex_font_size = etree.SubElement(run_properties, f"{{{_W_NS}}}szCs")
                complex_font_size.set(f"{{{_W_NS}}}val", str(round(size * 2)))
            if underline:
                underline_node = etree.SubElement(run_properties, f"{{{_W_NS}}}u")
                underline_node.set(f"{{{_W_NS}}}val", "single")
            text_node = etree.SubElement(run, f"{{{_W_NS}}}t")
            if text[:1].isspace() or text[-1:].isspace():
                text_node.set(f"{{{_XML_NS}}}space", "preserve")
            text_node.text = text

    paragraph.add_run()._r.append(pict)


def _add_binding_header(header, *, side: str) -> None:
    """Render the supplied reference's odd/even VML binding-line styles."""
    if side not in {"left", "right"}:
        raise ValueError(f"不支持的装订线方向：{side}")
    paragraph = header.paragraphs[0]
    for run in list(paragraph._p.findall(qn("w:r"))):
        paragraph._p.remove(run)
    paragraph.paragraph_format.space_after = Pt(0)

    if side == "left":
        _append_vml_textbox(
            paragraph,
            shape_id="_x0000_s1050",
            spid="_x0000_s1050",
            shape_style=(
                "position:absolute;left:0pt;margin-left:-88.1pt;margin-top:-27.8pt;"
                "height:838.15pt;width:84pt;mso-position-vertical-relative:line;"
                "z-index:251659264;mso-width-relative:page;mso-height-relative:page;"
            ),
            textbox_style="layout-flow:vertical-ideographic;",
            paragraph_specs=((
                "center",
                (
                    ("·" * 36, 5.5, False),
                    ("装", 5.5, False),
                    ("·" * 17, 5.5, False),
                    ("订", 5.5, False),
                    ("·" * 17, 5.5, False),
                    ("线", 5.5, False),
                    ("·" * 36, 5.5, False),
                ),
            ),),
        )
        _append_vml_textbox(
            paragraph,
            shape_id="_x0000_s1055",
            spid="_x0000_s1055",
            shape_style=(
                "position:absolute;left:0pt;margin-left:-58.5pt;margin-top:32.4pt;"
                "height:594.7pt;width:41.4pt;z-index:251660288;"
                "mso-width-relative:page;mso-height-relative:page;"
            ),
            textbox_style="layout-flow:vertical;mso-layout-flow-alt:bottom-to-top;",
            paragraph_specs=((
                "left",
                (
                    ("姓名：", 14, False),
                    (" " * 13, 14, True),
                    ("  班级：", 14, False),
                    (" " * 13, 14, True),
                    ("  学号：", 14, False),
                    (" " * 12, 14, True),
                ),
            ),),
        )
        return

    _append_vml_textbox(
        paragraph,
        shape_id="文本框 27",
        spid="_x0000_s1025",
        shape_style=(
            "position:absolute;left:0pt;margin-left:1023.75pt;margin-top:-32.1pt;"
            "height:839.9pt;width:39.3pt;mso-position-vertical-relative:line;"
            "z-index:251660288;mso-width-relative:page;mso-height-relative:page;"
        ),
        textbox_style="layout-flow:vertical-ideographic;",
        paragraph_specs=(
            ("center", (("装　　　订　　　线　　　内　　　不　　　要　　　答　　　题", None, False),)),
            ("center", (("．" * 116, 5.5, False),)),
        ),
    )


def _shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _prevent_row_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is None:
        tr_pr.append(OxmlElement("w:cantSplit"))


def _add_bottom_border(paragraph, *, color="D7DDE3", size="4") -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    bottom = p_bdr.find(qn("w:bottom"))
    if bottom is None:
        bottom = OxmlElement("w:bottom")
        p_bdr.append(bottom)
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), size)
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), color)


def _populate_page_number(footer) -> None:
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(paragraph.add_run("第 "), "宋体", 8.5, color=_MUTED)
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instr, end))
    _set_run_font(run, "宋体", 8.5, color=_MUTED)
    _set_run_font(paragraph.add_run(" 页"), "宋体", 8.5, color=_MUTED)


def _add_page_numbers(section) -> None:
    _populate_page_number(section.footer)


def _configure_styles(doc: DocumentObject) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(9.5)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.space_after = Pt(1)
    normal.paragraph_format.line_spacing = 1.05


def _add_exam_header(doc: DocumentObject, title: str, *, answer_version: bool, teaching_context=None) -> None:
    from services.teaching_context import stage_label

    subject = f"{stage_label(teaching_context)}数学"
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.space_after = Pt(2)
    _set_run_font(title_p.add_run(_clean_text(title) or f"{subject}练习"), "黑体", 15, bold=True)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(3)
    label = "答案解析版" if answer_version else "学生试卷版"
    _set_run_font(subtitle.add_run(label), "黑体", 9.5, bold=True, color=_BLUE)

    if not answer_version:
        return

    values = (
        f"科目：{subject}",
        "日期：____年__月__日",
        "考试时间：________分钟",
        "用途：讲评 / 备课",
    )
    info = doc.add_table(rows=1, cols=len(values))
    info.alignment = WD_TABLE_ALIGNMENT.CENTER
    info.autofit = True
    _set_table_borders(info, value="nil")
    for cell, value in zip(info.rows[0].cells, values):
        _set_cell_margins(cell, top=20, bottom=20)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run_font(paragraph.add_run(value), "宋体", 8.5)

    note = doc.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    note.paragraph_format.space_before = Pt(1)
    note.paragraph_format.space_after = Pt(2)
    note_text = "满分：________分    请在规定区域内作答"
    _set_run_font(
        note.add_run(note_text),
        "宋体",
        8.5,
        color=_MUTED,
    )
    _add_bottom_border(note, color="7C8996", size="6")


def _add_section_header(
    doc: DocumentObject,
    kind: str,
    count: int,
    *,
    continued: bool = False,
    page_break_before: bool = False,
) -> None:
    title, instruction = _TYPE_META[kind]
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.page_break_before = page_break_before
    count_label = f"（续，余 {count} 题）" if continued else f"（共 {count} 题）"
    _set_run_font(paragraph.add_run(f"{title}{count_label}"), "黑体", 10.5, bold=True, color=_BLUE)
    _set_run_font(paragraph.add_run(f"  {instruction}"), "宋体", 8, color=_MUTED)
    _add_bottom_border(paragraph, color="6B8799", size="8")


def _remove_choice_answer_slot(text: str) -> str:
    """Remove the source's empty marker before adding one aligned answer slot."""
    matches = list(_CHOICE_ANSWER_SLOT_RE.finditer(text))
    if not matches:
        return text
    marker = matches[-1]
    return f"{text[:marker.start()].rstrip()}{text[marker.end():]}".strip()


def _add_question_stem(
    doc: DocumentObject,
    number: int,
    text: str,
    *,
    keep_next: bool,
    choice_answer_slot: bool = False,
    page_break_before: bool = False,
    space_before: float = 2,
    line_spacing: float | None = None,
) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(1)
    paragraph.paragraph_format.keep_with_next = keep_next
    paragraph.paragraph_format.widow_control = True
    if line_spacing is not None:
        paragraph.paragraph_format.line_spacing = line_spacing
    if page_break_before:
        paragraph.paragraph_format.page_break_before = True
    if choice_answer_slot:
        text = _remove_choice_answer_slot(text)
    _set_run_font(paragraph.add_run(f"{number}. "), "黑体", 9.5, bold=True)
    _add_text(paragraph, text, size=9.5)
    if choice_answer_slot:
        # DOCX geometry intentionally differs from the broader OCR crop: the
        # 35 mm binding strip, 20 mm outer margin and 840 twips gutter define
        # this printable column.  The OCR 15%/63 mm left crop is coverage only.
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Mm(_COLUMN_WIDTH_MM),
            WD_TAB_ALIGNMENT.RIGHT,
        )
        _set_run_font(paragraph.add_run("\t（　）"), "宋体", 9.5)


def _add_choice_options(
    doc: DocumentObject,
    options: dict[str, str],
    *,
    line_spacing: float | None = None,
) -> None:
    option_values = [_clean_text(options.get(label, "")) for label in "ABCD"]
    compact = max((len(value) for value in option_values), default=0) <= 15 and sum(map(len, option_values)) <= 48
    rows, cols = (1, 4) if compact else (2, 2)
    table = doc.add_table(rows=rows, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    _set_table_borders(table, value="nil")
    for row in table.rows:
        _prevent_row_split(row)
    for index, label in enumerate("ABCD"):
        row_index = 0 if rows == 1 else index // 2
        col_index = index if rows == 1 else index % 2
        cell = table.cell(row_index, col_index)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _set_cell_margins(cell, top=10, bottom=10, start=35, end=35)
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        if line_spacing is not None:
            paragraph.paragraph_format.line_spacing = line_spacing
        _set_run_font(paragraph.add_run(f"{label}. "), "宋体", 9, bold=True)
        _add_text(paragraph, option_values[index], size=9)


def _add_question_table(doc: DocumentObject, rows: list[list[str]]) -> None:
    column_count = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=column_count)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _set_table_borders(table, value="single", color="7C8996", size="4")
    column_width = Mm(_COLUMN_WIDTH_MM / column_count)
    for column in table.columns:
        column.width = column_width
    for row_index, row in enumerate(table.rows):
        _prevent_row_split(row)
        for column_index, cell in enumerate(row.cells):
            cell.width = column_width
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _set_cell_margins(cell, top=35, bottom=35, start=30, end=30)
            if row_index == 0:
                _shade_cell(cell, "F3F7F9")
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            _add_text(
                paragraph,
                rows[row_index][column_index],
                size=8,
                bold=row_index == 0 or column_index == 0,
            )


def _blank_line_count(
    question: dict[str, Any],
    *,
    solution_count: int = 1,
    final_page_mode: str | None = None,
) -> int:
    """Budget writable space without forcing a standard A3 paper onto page 3."""
    stem_size = len(_clean_text(question.get("question", "")))
    if final_page_mode == "solo":
        return 28 if stem_size < 180 else 20
    if final_page_mode == "shared":
        return 16 if stem_size < 180 else 12
    if final_page_mode == "upper_pair":
        return 16 if stem_size < 180 else 12
    if final_page_mode == "lower_pair":
        return 14 if stem_size < 180 else 10
    if solution_count >= 5:
        return 11 if stem_size < 180 else 8
    if solution_count == 4:
        return 13 if stem_size < 180 else 10
    if solution_count == 3:
        return 16 if stem_size < 180 else 12
    if solution_count == 2:
        return 20 if stem_size < 180 else 15
    return 28 if stem_size < 180 else 20


def _add_solution_space(doc: DocumentObject, lines: int) -> None:
    for _ in range(lines):
        paragraph = doc.add_paragraph(" ")
        paragraph.paragraph_format.space_after = Pt(1.5)
        paragraph.paragraph_format.line_spacing = 0.9
        _add_bottom_border(paragraph, color="D7DDE3", size="3")


def _add_answer_block(doc: DocumentObject, question: dict[str, Any]) -> None:
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    _set_table_borders(table, value="single", color="C6D5DF", size="4")
    cell = table.cell(0, 0)
    _shade_cell(cell, "F3F7F9")
    _set_cell_margins(cell, top=70, bottom=70, start=90, end=90)
    first = cell.paragraphs[0]
    first.paragraph_format.space_after = Pt(2)
    _set_run_font(first.add_run("答案："), "黑体", 9.5, bold=True, color=_BLUE)
    _add_text(first, question.get("answer", ""), size=9.5)

    solution = cell.add_paragraph()
    solution.paragraph_format.space_after = Pt(2)
    _set_run_font(solution.add_run("解析："), "黑体", 9.5, bold=True, color=_BLUE)
    _add_text(solution, question.get("solution", ""), size=9.5)

    knowledge = cell.add_paragraph()
    knowledge.paragraph_format.space_after = Pt(0)
    _set_run_font(knowledge.add_run("知识点："), "黑体", 8.5, bold=True, color=_MUTED)
    _add_text(knowledge, question.get("knowledge_point", "综合"), size=8.5, color=_MUTED)


def _estimated_question_weight(
    question: dict[str, Any],
    *,
    answer_version: bool,
    solution_count: int = 1,
) -> float:
    """Estimate rendered height so short papers still use both Word columns."""
    kind = question.get("type")
    stem_weight = 0.9 + len(_clean_text(question.get("question", ""))) / 90
    if answer_version:
        answer_weight = 1.2 + len(_clean_text(question.get("solution", ""))) / 75
        return stem_weight + answer_weight
    if kind == "choice":
        options = question.get("options", {})
        option_size = sum(len(_clean_text(options.get(label, ""))) for label in "ABCD")
        return stem_weight + (0.8 if option_size <= 48 else 1.4)
    if kind == "solution":
        return stem_weight + _blank_line_count(
            question,
            solution_count=solution_count,
        ) * 0.78
    return stem_weight + 0.35


def _balanced_column_break_index(
    questions: list[dict[str, Any]],
    *,
    answer_version: bool,
) -> int | None:
    """Choose a non-empty right-column start, preferring a question-type boundary."""
    if len(questions) < 2:
        return None
    solution_count = sum(question.get("type") == "solution" for question in questions)
    weights = [
        _estimated_question_weight(
            question,
            answer_version=answer_version,
            solution_count=solution_count,
        )
        for question in questions
    ]
    total = sum(weights)
    # Long papers should flow through Word's columns and pages naturally. A
    # forced column break is only needed to visually balance short one-page
    # papers; applying it to a long paper can compress or distort pagination.
    if total > 48:
        return None
    prefix = 0.0
    candidates: list[tuple[float, int]] = []
    for index in range(1, len(questions)):
        prefix += weights[index - 1]
        imbalance = abs(prefix - total / 2)
        at_type_boundary = questions[index - 1].get("type") != questions[index].get("type")
        boundary_bonus = total * 0.08 if at_type_boundary else 0.0
        edge_penalty = total if prefix < total * 0.18 or prefix > total * 0.82 else 0.0
        candidates.append((imbalance - boundary_bonus + edge_penalty, index))
    return min(candidates)[1]


def _add_column_break(doc: DocumentObject) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.add_run().add_break(WD_BREAK.COLUMN)


def _build_document(title: str, questions: Iterable[Any], *, answer_version: bool, teaching_context=None) -> bytes:
    normalized = [_as_dict(question) for question in questions]
    if not normalized:
        raise ValueError("至少需要一道题才能生成试卷")

    doc = Document()
    from services.teaching_context import stage_label

    doc.core_properties.title = _clean_text(title) or f"{stage_label(teaching_context)}数学练习"
    doc.core_properties.subject = "答案解析" if answer_version else "学生试卷"
    doc.core_properties.author = "MathAgent"
    _configure_styles(doc)

    header_section = doc.sections[0]
    _set_page(header_section, binding_side="left")
    _set_columns(header_section, 1)
    _add_page_numbers(header_section)
    if not answer_version:
        _add_binding_header(header_section.header, side="left")
    _add_exam_header(doc, title, answer_version=answer_version, teaching_context=teaching_context)

    body_section = doc.add_section(WD_SECTION.CONTINUOUS)
    _set_page(body_section, binding_side="left")
    _set_columns(body_section, 2, separator=True)

    grouped = {
        kind: [question for question in normalized if question.get("type") == kind]
        for kind in _TYPE_ORDER
    }
    solution_count = len(grouped["solution"])
    standard_two_page_student = (
        not answer_version
        and len(grouped["choice"]) == 11
        and len(grouped["fill"]) == 3
        and solution_count == 5
    )
    ordered = [question for kind in _TYPE_ORDER for question in grouped[kind]]
    estimated_total = sum(
        _estimated_question_weight(
            question,
            answer_version=answer_version,
            solution_count=solution_count,
        )
        for question in ordered
    )
    # ``_balanced_column_break_index`` uses a deliberately low threshold for
    # short-paper balancing.  That threshold is not the same as the point at
    # which an explicit solution-page break becomes redundant.  Only sizeable
    # papers should suppress it; otherwise the established compact layouts
    # would lose their intentional break.
    long_paper = len(ordered) >= 24 or estimated_total > 100
    break_index = (
        None
        if standard_two_page_student
        else _balanced_column_break_index(ordered, answer_version=answer_version)
    )
    number = 1
    global_index = 0
    second_page_left = None
    second_page_right = None
    for kind in _TYPE_ORDER:
        section_questions = grouped[kind]
        if not section_questions:
            continue
        section_container = doc
        if standard_two_page_student and kind == "fill":
            second_page_section = doc.add_section(WD_SECTION.NEW_PAGE)
            _set_page(second_page_section, binding_side="right")
            _set_columns(second_page_section, 1)
            second_page_section.header.is_linked_to_previous = False
            second_page_section.footer.is_linked_to_previous = False
            _add_binding_header(second_page_section.header, side="right")
            _add_page_numbers(second_page_section)
            second_page_left, second_page_right = _add_two_column_question_table(doc)
            section_container = second_page_left
        elif standard_two_page_student and kind == "solution":
            section_container = second_page_left
        if break_index == global_index and global_index > 0:
            _add_column_break(section_container)
        _add_section_header(section_container, kind, len(section_questions))
        for section_index, question in enumerate(section_questions):
            question_container = section_container
            if standard_two_page_student and kind == "choice" and section_index == 8:
                _add_column_break(doc)
            elif standard_two_page_student and kind == "solution" and section_index >= 3:
                question_container = second_page_right
            elif break_index == global_index and section_index > 0:
                _add_column_break(section_container)
                _add_section_header(
                    section_container,
                    kind,
                    len(section_questions) - section_index,
                    continued=True,
                )
            question_parts = _extract_question_parts(question.get("question", ""))
            text_parts = [
                str(content)
                for part_kind, content in question_parts
                if part_kind == "text"
            ]
            text = text_parts[0] if text_parts else ""
            if kind == "fill" and not re.search(r"_{3,}|（\s*）|\(\s*\)", text):
                text = f"{text}　____________"
            _add_question_stem(
                question_container,
                number,
                text,
                keep_next=(kind == "choice"),
                choice_answer_slot=(not answer_version and kind == "choice"),
                page_break_before=(
                    not answer_version
                    and not standard_two_page_student
                    and not long_paper
                    and kind == "solution"
                    and section_index == 1
                ),
                space_before=2,
                line_spacing=(
                    1.5
                    if not answer_version and kind in {"choice", "fill"}
                    else None
                ),
            )
            # Render every extracted table/text fragment in source order.  A
            # question may contain a main data table followed by an appendix;
            # keeping the order avoids moving the appendix ahead of subparts.
            stem_consumed = False
            for part_kind, content in question_parts:
                if part_kind == "text" and not stem_consumed:
                    stem_consumed = True
                    continue
                if part_kind == "table":
                    table_rows = content
                    assert isinstance(table_rows, list)
                    _add_question_table(question_container, table_rows)
                    continue
                trailing_text = str(content).strip()
                if trailing_text:
                    trailing = question_container.add_paragraph()
                    trailing.paragraph_format.space_after = Pt(1)
                    _add_text(trailing, trailing_text, size=9.5)
            if kind == "choice":
                _add_choice_options(
                    question_container,
                    question.get("options", {}),
                    line_spacing=1.5 if not answer_version else None,
                )
            if answer_version:
                _add_answer_block(question_container, question)
            elif kind == "solution":
                solution_lines = _blank_line_count(
                    question,
                    solution_count=solution_count,
                    final_page_mode=(
                        "upper_pair"
                        if standard_two_page_student and section_index == 3
                        else "lower_pair"
                        if standard_two_page_student and section_index == 4
                        else None
                    ),
                )
                # On a long paper Word can otherwise push the final writable
                # line onto a page by itself. Keep the final block compact;
                # short papers retain the established answer-space budget.
                if (
                    not answer_version
                    and long_paper
                    and not standard_two_page_student
                    and section_index == len(section_questions) - 1
                ):
                    solution_lines = max(1, solution_lines - 6)
                _add_solution_space(
                    question_container,
                    solution_lines,
                )
            number += 1
            global_index += 1

    _hide_square_root_degree_placeholders(doc.element)
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


def build_exercise_documents(title: str, questions: Iterable[Any], *, teaching_context=None) -> tuple[bytes, bytes]:
    """Return ``(student_paper_docx, answer_key_docx)`` as editable DOCX bytes."""
    from services.teaching_context import validate_teaching_context

    snapshot = validate_teaching_context(teaching_context)
    normalized = list(questions)
    paper = _build_document(title, normalized, answer_version=False, teaching_context=snapshot)
    answer = _build_document(title, normalized, answer_version=True, teaching_context=snapshot)
    return paper, answer
