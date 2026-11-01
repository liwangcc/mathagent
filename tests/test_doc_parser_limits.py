from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import doc_parser


class _Document:
    def __init__(self, pages):
        self._pages = pages
        self.page_count = len(pages)
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def load_page(self, index):
        return self._pages[index]

    def close(self):
        self.closed = True


def test_pdf_page_count_is_rejected_before_scanning(monkeypatch):
    document = _Document([object()] * (doc_parser._MAX_PDF_PAGES + 1))
    monkeypatch.setattr(
        doc_parser,
        "fitz",
        SimpleNamespace(open=lambda **_kwargs: document),
    )

    with pytest.raises(ValueError, match="页数过多"):
        doc_parser._parse_pdf(b"pdf")

    assert document.closed is True


def test_scanned_pdf_is_scaled_before_pixmap_allocation(monkeypatch):
    observed = {}

    class Page:
        rect = SimpleNamespace(width=4_000.0, height=4_000.0)

        def get_text(self):
            return ""

        def get_pixmap(self, *, matrix, alpha):
            observed["zoom"] = matrix[0]
            observed["alpha"] = alpha
            edge = round(4_000 * matrix[0])
            return SimpleNamespace(
                width=edge,
                height=edge,
                tobytes=lambda _format: b"png",
            )

    document = _Document([Page()])
    monkeypatch.setattr(
        doc_parser,
        "fitz",
        SimpleNamespace(
            open=lambda **_kwargs: document,
            Matrix=lambda x, y: (x, y),
        ),
    )

    result = doc_parser._parse_pdf(b"pdf")

    assert result["kind"] == "image"
    assert observed == {"zoom": 1.0, "alpha": False}
    assert document.closed is True


def test_image_can_remain_bytes_until_downstream_memory_admission():
    raw = b"image-bytes"

    result = doc_parser.parse_document(
        raw,
        "roster.png",
        "image/png",
        encode_images=False,
    )

    assert result == {
        "kind": "image",
        "image_bytes": raw,
        "mime": "image/png",
    }


def test_extreme_pdf_page_geometry_is_rejected_before_render(monkeypatch):
    class Page:
        rect = SimpleNamespace(width=20_001.0, height=100.0)

        def get_text(self):
            return ""

        def get_pixmap(self, **_kwargs):
            raise AssertionError("oversized page must not be rendered")

    document = _Document([Page()])
    monkeypatch.setattr(
        doc_parser,
        "fitz",
        SimpleNamespace(
            open=lambda **_kwargs: document,
            Matrix=lambda x, y: (x, y),
        ),
    )

    with pytest.raises(ValueError, match="页面尺寸"):
        doc_parser._parse_pdf(b"pdf")

    assert document.closed is True


def test_office_archive_rejects_excessive_expanded_total(monkeypatch):
    class Info:
        file_size = doc_parser._MAX_OFFICE_ARCHIVE_TOTAL_BYTES + 1
        compress_size = file_size

        def is_dir(self):
            return False

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def infolist(self):
            return [Info()]

    monkeypatch.setattr(doc_parser.zipfile, "ZipFile", lambda *_args: Archive())

    with pytest.raises(ValueError, match="单项展开后过大|总体积过大"):
        doc_parser._validate_office_archive(b"zip")


def test_office_archive_rejects_suspicious_compression_ratio(monkeypatch):
    class Info:
        file_size = 2 * 1024 * 1024
        compress_size = 100

        def is_dir(self):
            return False

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def infolist(self):
            return [Info()]

    monkeypatch.setattr(doc_parser.zipfile, "ZipFile", lambda *_args: Archive())

    with pytest.raises(ValueError, match="压缩比例异常"):
        doc_parser._validate_office_archive(b"zip")


def test_xlsx_output_is_bounded_during_iteration_and_workbook_closes(monkeypatch):
    class Worksheet:
        title = "large"
        max_column = doc_parser._MAX_XLSX_COLUMNS

        def iter_rows(self, **_kwargs):
            yield tuple("x" * 5000 for _ in range(self.max_column))

    class Workbook:
        worksheets = [Worksheet()]
        closed = False

        def close(self):
            self.closed = True

    workbook = Workbook()
    monkeypatch.setattr(
        doc_parser.openpyxl,
        "load_workbook",
        lambda *_args, **_kwargs: workbook,
    )

    result = doc_parser._parse_xlsx(b"xlsx")

    assert len(result) < doc_parser._MAX_TEXT_CHARS + 100
    assert "内容过长已截断" in result
    assert workbook.closed is True


def test_xlsx_workbook_closes_when_row_iteration_fails(monkeypatch):
    class Worksheet:
        title = "broken"
        max_column = 1

        def iter_rows(self, **_kwargs):
            raise RuntimeError("synthetic iteration failure")

    class Workbook:
        worksheets = [Worksheet()]
        closed = False

        def close(self):
            self.closed = True

    workbook = Workbook()
    monkeypatch.setattr(
        doc_parser.openpyxl,
        "load_workbook",
        lambda *_args, **_kwargs: workbook,
    )

    with pytest.raises(RuntimeError, match="synthetic iteration failure"):
        doc_parser._parse_xlsx(b"xlsx")

    assert workbook.closed is True


def test_docx_stops_before_visiting_tables_after_text_budget(monkeypatch):
    class Paragraph:
        text = "x" * doc_parser._MAX_CELL_CHARS
        style = None
        runs = []

    class Document:
        paragraphs = [Paragraph()] * 20

        @property
        def tables(self):
            raise AssertionError("tables must not be expanded after text budget")

    monkeypatch.setattr(doc_parser, "Document", lambda _stream: Document())

    result = doc_parser._parse_docx(b"docx")

    assert len(result) < doc_parser._MAX_TEXT_CHARS + 100
    assert "内容过长已截断" in result
