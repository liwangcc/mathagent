"""文件解析服务 — 阶段四：自动分流的核心

老师传任意文件，本模块按类型分派：
- Office（docx/xlsx/pptx）/ 可复制 PDF → 直接解析出带格式的 Markdown 文本
  （结构化信息存在文件内部，后端解析比识图更准、零视觉 token）。
- 图片 / 扫描件 PDF（fitz 提取文字为空）→ 渲染/透传成图，交给识图通道。

红线：本模块**不调用任何外部 API**，纯本地解析。调用方（routers/vision_office.py）
把图片交给 ocr_service.read_image（qwen3.8-flash 识图），文本直接交给 Hermes。

统一入口 parse_document() 返回：
- {"kind": "text",  "content": <Markdown>, "source_format": "docx|xlsx|pptx|pdf"}
- {"kind": "image", "image_b64": <b64>, "mime": "image/png|image/jpeg|..."}
  Callers that own a downstream memory gate may pass ``encode_images=False``
  and receive ``image_bytes`` instead.
"""

import base64
import io
import logging
import math
import zipfile

logger = logging.getLogger(__name__)

# Optional parsers stay unloaded until their file type is actually used. These
# names also remain injectable for isolated parser tests.
Document = None
Presentation = None
fitz = None


class _LazyOpenpyxl:
    @staticmethod
    def load_workbook(*args, **kwargs):
        try:
            import openpyxl as module
        except ImportError as exc:
            raise DocumentParserUnavailableError("openpyxl is unavailable") from exc
        return module.load_workbook(*args, **kwargs)


openpyxl = _LazyOpenpyxl()


def _docx_document():
    global Document
    if Document is None:
        try:
            from docx import Document as document_factory
        except ImportError as exc:
            raise DocumentParserUnavailableError(
                "python-docx is unavailable"
            ) from exc
        Document = document_factory
    return Document


def _openpyxl_module():
    return openpyxl


def _presentation_factory():
    global Presentation
    if Presentation is None:
        try:
            from pptx import Presentation as presentation_factory
        except ImportError as exc:
            raise DocumentParserUnavailableError("python-pptx is unavailable") from exc
        Presentation = presentation_factory
    return Presentation


def _fitz_module():
    global fitz
    if fitz is None:
        try:
            import fitz as module  # PyMuPDF
        except ImportError as exc:
            raise DocumentParserUnavailableError("PyMuPDF is unavailable") from exc
        fitz = module
    return fitz

# 文本截断上限（字符），控 token；超出截断并标注
_MAX_TEXT_CHARS = 12000
# Excel 单 sheet 最大行数，防超大表爆 token
_MAX_XLSX_ROWS = 200
# 渲染扫描件 PDF 的缩放（2.0 ≈ 144dpi，够清晰又不过大）
_PDF_RENDER_ZOOM = 2.0
_MAX_PDF_PAGES = 200
_MAX_PDF_RENDER_PIXELS = 16_000_000
_MAX_PDF_PAGE_POINTS = 20_000.0
_MAX_PDF_RENDER_BYTES = 25 * 1024 * 1024
_MAX_OFFICE_ARCHIVE_MEMBERS = 4096
_MAX_OFFICE_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_OFFICE_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_OFFICE_COMPRESSION_RATIO = 200
_MAX_XLSX_SHEETS = 100
_MAX_XLSX_COLUMNS = 256
_MAX_PPTX_SLIDES = 200
_MAX_PPTX_SHAPES_PER_SLIDE = 1000
_MAX_DOCX_PARAGRAPHS = 5000
_MAX_DOCX_TABLES = 200
_MAX_TABLE_ROWS = 1000
_MAX_TABLE_COLUMNS = 256
_MAX_CELL_CHARS = 1000
_MAX_PPTX_PARAGRAPHS_PER_SHAPE = 1000
_MAX_PPTX_RUNS_PER_PARAGRAPH = 1000

_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif",
}
_OFFICE_EXT = {"docx", "xlsx", "pptx"}


class DocumentParserUnavailableError(RuntimeError):
    """A server-side parser dependency is unavailable."""


def _ext_of(filename: str) -> str:
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _truncate(text: str) -> str:
    if len(text) > _MAX_TEXT_CHARS:
        return text[:_MAX_TEXT_CHARS] + "\n\n…（内容过长已截断）"
    return text


def _append_bounded(
    parts: list[str],
    text: str,
    used: int,
    *,
    separator: str,
) -> tuple[int, bool]:
    """Append at most one model-sized text window while parsing."""
    value = str(text or "")
    if not value:
        return used, False
    prefix = separator if parts else ""
    remaining = _MAX_TEXT_CHARS + 1 - used
    if remaining <= 0:
        return used, True
    addition = prefix + value
    if len(addition) > remaining:
        parts.append(addition[:remaining])
        return used + remaining, True
    parts.append(addition)
    used += len(addition)
    return used, used > _MAX_TEXT_CHARS


def _bounded_cell(value: object) -> str:
    return str(value or "").strip().replace("\n", " ")[:_MAX_CELL_CHARS]


def _finish_bounded(parts: list[str], *, truncated: bool, empty: str) -> str:
    output = "".join(parts).strip()
    if not output:
        return empty
    if truncated or len(output) > _MAX_TEXT_CHARS:
        return output[:_MAX_TEXT_CHARS] + "\n\n…（内容过长已截断）"
    return output


def _validate_office_archive(raw: bytes) -> None:
    """Reject Office ZIP bombs before document libraries expand XML/media."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("Office 文件损坏或格式无效") from exc
    if len(members) > _MAX_OFFICE_ARCHIVE_MEMBERS:
        raise ValueError("Office 文件包含的内部项目过多")
    total_size = 0
    for member in members:
        if member.is_dir():
            continue
        file_size = max(0, int(member.file_size))
        compressed_size = max(0, int(member.compress_size))
        if file_size > _MAX_OFFICE_ARCHIVE_MEMBER_BYTES:
            raise ValueError("Office 文件内部单项展开后过大")
        total_size += file_size
        if total_size > _MAX_OFFICE_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Office 文件展开后总体积过大")
        if (
            file_size > 1024 * 1024
            and file_size > max(1, compressed_size) * _MAX_OFFICE_COMPRESSION_RATIO
        ):
            raise ValueError("Office 文件压缩比例异常")


def _image_result(raw: bytes, mime: str, *, encode_images: bool) -> dict:
    if encode_images:
        return {
            "kind": "image",
            "image_b64": base64.b64encode(raw).decode(),
            "mime": mime,
        }
    return {"kind": "image", "image_bytes": raw, "mime": mime}


def parse_document(
    raw: bytes,
    filename: str,
    content_type: str = "",
    *,
    encode_images: bool = True,
) -> dict:
    """统一入口：按文件类型分派，返回 text 或 image 结构。

    解析失败 / 不支持的类型 → 抛 ValueError，由调用方转成 4xx。
    """
    ext = _ext_of(filename)

    # 图片：直接透传给识图通道
    if ext in _IMAGE_MIME:
        return _image_result(raw, _IMAGE_MIME[ext], encode_images=encode_images)
    if content_type.startswith("image/"):
        return _image_result(raw, content_type, encode_images=encode_images)

    if ext == "docx":
        _validate_office_archive(raw)
        return {"kind": "text", "content": _truncate(_parse_docx(raw)), "source_format": "docx"}
    if ext == "xlsx":
        _validate_office_archive(raw)
        return {"kind": "text", "content": _truncate(_parse_xlsx(raw)), "source_format": "xlsx"}
    if ext == "pptx":
        _validate_office_archive(raw)
        return {"kind": "text", "content": _truncate(_parse_pptx(raw)), "source_format": "pptx"}
    if ext == "pdf":
        return _parse_pdf(raw, encode_images=encode_images)

    raise ValueError(f"不支持的文件类型：.{ext or '未知'}（支持 docx/xlsx/pptx/pdf 或图片）")


def _parse_docx(raw: bytes) -> str:
    """Word → Markdown：保留 heading 级别、加粗、表格。"""
    doc = _docx_document()(io.BytesIO(raw))
    parts: list[str] = []
    used = 0
    truncated = False

    for paragraph_index, para in enumerate(doc.paragraphs):
        if paragraph_index >= _MAX_DOCX_PARAGRAPHS:
            truncated = True
            break
        text = (para.text or "").strip()[:_MAX_CELL_CHARS]
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style else ""
        if "heading 1" in style or style == "title":
            value = f"# {text}"
        elif "heading 2" in style:
            value = f"## {text}"
        elif "heading" in style:
            value = f"### {text}"
        else:
            # 整段加粗 → **加粗**
            runs = [r for r in para.runs if (r.text or "").strip()]
            if runs and all(r.bold for r in runs):
                value = f"**{text}**"
            else:
                value = text
        used, truncated = _append_bounded(
            parts,
            value,
            used,
            separator="\n\n",
        )
        if truncated:
            break

    for table_index, tbl in enumerate(doc.tables if not truncated else ()):
        if table_index >= _MAX_DOCX_TABLES:
            truncated = True
            break
        rows = []
        table_chars = 0
        for row_index, row in enumerate(tbl.rows):
            if row_index >= _MAX_TABLE_ROWS:
                truncated = True
                break
            cells = [
                _bounded_cell(cell.text)
                for cell in row.cells[:_MAX_TABLE_COLUMNS]
            ]
            row_chars = sum(len(cell) + 3 for cell in cells)
            if table_chars + row_chars > _MAX_TEXT_CHARS + 1:
                truncated = True
                break
            table_chars += row_chars
            rows.append(cells)
        used, output_full = _append_bounded(
            parts,
            _rows_to_md_table(rows),
            used,
            separator="\n\n",
        )
        truncated = truncated or output_full
        if truncated:
            break

    return _finish_bounded(parts, truncated=truncated, empty="（空文档）")


def _parse_xlsx(raw: bytes) -> str:
    """Excel → 每个 sheet 一段 Markdown 表格。"""
    wb = _openpyxl_module().load_workbook(
        io.BytesIO(raw), read_only=True, data_only=True
    )
    parts: list[str] = []
    used = 0
    truncated = False
    try:
        worksheets = wb.worksheets
        if len(worksheets) > _MAX_XLSX_SHEETS:
            raise ValueError("Excel 工作表数量过多")
        for ws in worksheets:
            used, truncated = _append_bounded(
                parts,
                f"## 工作表：{str(ws.title)[:200]}",
                used,
                separator="\n\n",
            )
            if truncated:
                break
            rows = []
            table_chars = 0
            max_column = min(max(1, int(ws.max_column or 1)), _MAX_XLSX_COLUMNS)
            for i, row in enumerate(ws.iter_rows(
                min_row=1,
                max_row=_MAX_XLSX_ROWS + 1,
                min_col=1,
                max_col=max_column,
                values_only=True,
            )):
                if i >= _MAX_XLSX_ROWS:
                    truncated = True
                    break
                cells = [_bounded_cell(value) for value in row]
                if not any(cells):
                    continue
                row_chars = sum(len(cell) + 3 for cell in cells)
                if table_chars + row_chars > _MAX_TEXT_CHARS + 1:
                    truncated = True
                    break
                table_chars += row_chars
                rows.append(cells)
            if rows:
                used, output_full = _append_bounded(
                    parts,
                    _rows_to_md_table(rows),
                    used,
                    separator="\n\n",
                )
                truncated = truncated or output_full
            if truncated:
                break
        return _finish_bounded(parts, truncated=truncated, empty="（空表格）")
    finally:
        wb.close()


def _parse_pptx(raw: bytes) -> str:
    """PPT → 每页标题 + 文本框内容。"""
    prs = _presentation_factory()(io.BytesIO(raw))
    parts: list[str] = []
    used = 0
    truncated = False
    if len(prs.slides) > _MAX_PPTX_SLIDES:
        raise ValueError("PPT 页数过多")
    for idx, slide in enumerate(prs.slides, start=1):
        used, truncated = _append_bounded(
            parts,
            f"## 第 {idx} 页",
            used,
            separator="\n",
        )
        if truncated:
            break
        for shape_index, shape in enumerate(slide.shapes):
            if shape_index >= _MAX_PPTX_SHAPES_PER_SLIDE:
                truncated = True
                break
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph_index, para in enumerate(shape.text_frame.paragraphs):
                if paragraph_index >= _MAX_PPTX_PARAGRAPHS_PER_SHAPE:
                    truncated = True
                    break
                remaining = _MAX_CELL_CHARS
                run_text: list[str] = []
                for run_index, run in enumerate(para.runs):
                    if run_index >= _MAX_PPTX_RUNS_PER_PARAGRAPH:
                        truncated = True
                        break
                    value = str(run.text or "")[:remaining]
                    run_text.append(value)
                    remaining -= len(value)
                    if remaining <= 0:
                        truncated = True
                        break
                text = "".join(run_text).strip()
                if text:
                    used, output_full = _append_bounded(
                        parts,
                        f"- {text}",
                        used,
                        separator="\n",
                    )
                    truncated = truncated or output_full
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break
    return _finish_bounded(parts, truncated=truncated, empty="（空演示文稿）")


def _parse_pdf(raw: bytes, *, encode_images: bool = True) -> dict:
    """PDF：fitz 提取文字。非空→文本 Markdown；空→判定扫描件，渲染首页成 PNG 走识图。"""
    runtime = _fitz_module()
    doc = runtime.open(stream=raw, filetype="pdf")
    try:
        if doc.page_count > _MAX_PDF_PAGES:
            raise ValueError("PDF 页数过多，无法安全解析")
        text_parts = []
        text_chars = 0
        for page in doc:
            t = (page.get_text() or "").strip()
            if t:
                remaining = _MAX_TEXT_CHARS + 1 - text_chars
                if remaining <= 0:
                    break
                bounded = t[:remaining]
                text_parts.append(bounded)
                text_chars += len(bounded)
                if text_chars > _MAX_TEXT_CHARS:
                    break
        full = "\n\n".join(text_parts).strip()

        if full:  # 可复制 PDF → 文本通道
            return {"kind": "text", "content": _truncate(full), "source_format": "pdf"}

        # 扫描件/纯图片 PDF → 渲染首页成 PNG 走识图
        if doc.page_count == 0:
            raise ValueError("PDF 没有可解析的页面")
        page = doc.load_page(0)
        width = float(page.rect.width)
        height = float(page.rect.height)
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0
            or height <= 0
            or width > _MAX_PDF_PAGE_POINTS
            or height > _MAX_PDF_PAGE_POINTS
        ):
            raise ValueError("PDF 页面尺寸无效或过大")
        render_zoom = min(
            _PDF_RENDER_ZOOM,
            math.sqrt(_MAX_PDF_RENDER_PIXELS / (width * height)),
        )
        mat = runtime.Matrix(render_zoom, render_zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        if pix.width * pix.height > _MAX_PDF_RENDER_PIXELS:
            raise ValueError("PDF 页面渲染尺寸过大")
        png_bytes = pix.tobytes("png")
        if len(png_bytes) > _MAX_PDF_RENDER_BYTES:
            raise ValueError("PDF 页面渲染结果过大")
        logger.info("PDF 判定为扫描件，渲染首页成图走识图（%d 页）", doc.page_count)
        return _image_result(
            png_bytes,
            "image/png",
            encode_images=encode_images,
        )
    finally:
        doc.close()


def _rows_to_md_table(rows: list[list[str]]) -> str:
    """二维数组 → Markdown 表格。首行作表头；空则返回空串。"""
    rows = [r for r in rows if r]
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    norm = [list(r) + [""] * (ncols - len(r)) for r in rows]

    def _fmt(cells):
        return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"

    header = _fmt(norm[0])
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    body = "\n".join(_fmt(r) for r in norm[1:])
    return "\n".join([header, sep, body]).strip()
