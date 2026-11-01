"""Render a ``.paper.txt`` transcription to a print-ready A4 PDF.

Tooling only (not imported by the web app).  Needs a separate tooling venv with
``pymupdf`` and ``playwright``; printing uses the locally installed Microsoft
Edge (``channel="msedge"``) so no browser download is required.

    python -m scripts.past_papers.render_paper --paper-dir <dir> --katex-dir <dir>

Outputs under ``<paper-dir>/build/<kind>/``: ``<kind>.html``, ``<kind>.pdf``,
``preview/page-N.png`` (teacher preview), ``check/page-N.png`` (150 dpi images
for page-by-page comparison) and ``render_report.json``.  The report records
KaTeX errors, overflowing blocks, broken images and font embedding; a non-empty
``blocking`` list means the PDF must not be published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.past_papers.paper_format import paper_html, parse_paper  # noqa: E402

FOOTER = (
    '<div style="width:100%;text-align:center;font-size:9pt;'
    'font-family:SimSun,serif;color:#000;">{label}　第 <span class="pageNumber"></span> 页'
    '（共 <span class="totalPages"></span> 页）</div>'
)
MARGINS = {"top": "16mm", "bottom": "18mm", "left": "18mm", "right": "18mm"}
CONTENT_WIDTH_PX = int((210 - 18 - 18) * 96 / 25.4)
PREVIEW_DPI = 110
CHECK_DPI = 150
_PT_PER_MM = 72 / 25.4
_FOOTER_TEXT_RE = re.compile(r"^[\s数学试题参考答案第共页（）0-9]+$")


def margin_marks(doc, tolerance_pt: float = 1.0) -> list:
    """Ink outside the printable area other than the footer.

    Chrome paints a first line's overflow (e.g. a tall fraction) into the
    previous page when a page break falls right above it, leaving a stray
    glyph fragment above the footer; anything in the margins is a print defect.
    """
    top_limit = float(MARGINS["top"].rstrip("m")) * _PT_PER_MM - tolerance_pt
    found = []
    for index, page in enumerate(doc, start=1):
        bottom_limit = page.rect.height - float(MARGINS["bottom"].rstrip("m")) * _PT_PER_MM + tolerance_pt
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    y0, y1 = span["bbox"][1], span["bbox"][3]
                    if text and (y0 < top_limit or (y1 > bottom_limit and not _FOOTER_TEXT_RE.match(text))):
                        found.append({"page": index, "text": text, "y": [round(y0, 1), round(y1, 1)]})
        for drawing in page.get_drawings():
            rect = drawing["rect"]
            if rect.y0 < top_limit or rect.y1 > bottom_limit:
                found.append({"page": index, "drawing": [round(v, 1) for v in rect]})
    return found

_LAYOUT_PROBE = """
() => {
  const issues = [];
  document.querySelectorAll('section.q, .dm, table.grid, .choices, h2.sec, h3.sec, p').forEach(el => {
    if (el.scrollWidth > el.clientWidth + 1) {
      const q = el.closest('section.q');
      issues.push({kind: 'overflow', q: q ? q.dataset.q : null, tag: el.tagName, over: el.scrollWidth - el.clientWidth});
    }
  });
  document.querySelectorAll('img').forEach(img => {
    if (!img.complete || !img.naturalWidth) {
      const q = img.closest('section.q');
      issues.push({kind: 'broken_image', q: q ? q.dataset.q : null, src: img.getAttribute('src')});
    }
  });
  return {issues, errors: window.__renderErrors || []};
}
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(paper_dir: Path, katex_dir: Path, *, kind: str = "student", footer_label: str = "数学试题",
           metadata: dict | None = None) -> dict:
    import pymupdf
    from playwright.sync_api import sync_playwright

    source_path = paper_dir / f"{kind}.paper.txt"
    paper = parse_paper(source_path.read_text(encoding="utf-8"))
    figures = {}
    figures_path = paper_dir / "figures.json"
    if figures_path.exists():
        figures = json.loads(figures_path.read_text(encoding="utf-8")).get("figures", {})
    out_dir = paper_dir / "build" / kind
    (out_dir / "preview").mkdir(parents=True, exist_ok=True)
    (out_dir / "check").mkdir(parents=True, exist_ok=True)
    for stale in list((out_dir / "preview").glob("*.png")) + list((out_dir / "check").glob("*.png")):
        stale.unlink()
    # Figures are referenced relative to the paper directory.
    figures_rel = {key: {**value, "file": "../../" + value["file"]} for key, value in figures.items()}
    html_text = paper_html(paper, figures=figures_rel, katex_dir=katex_dir.resolve().as_uri())
    html_path = out_dir / f"{kind}.html"
    html_path.write_text(html_text, encoding="utf-8")
    raw_pdf = out_dir / f"{kind}.raw.pdf"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            # Lay out at the printed content width (A4 210 mm minus side
            # margins) so option columns measured on screen hold on paper.
            page = browser.new_page(viewport={"width": CONTENT_WIDTH_PX, "height": 1123})
            page.emulate_media(media="print")
            page.goto(html_path.resolve().as_uri())
            page.wait_for_function("window.__layoutDone === true", timeout=30000)
            probe = page.evaluate(_LAYOUT_PROBE)
            page.pdf(path=str(raw_pdf), format="A4", margin=MARGINS, print_background=True,
                     display_header_footer=True, header_template="<span></span>",
                     footer_template=FOOTER.format(label=footer_label))
        finally:
            browser.close()
    pdf_path = out_dir / f"{kind}.pdf"
    doc = pymupdf.open(raw_pdf)
    # Attribution travels inside the file (document info), never on the page.
    providers = sorted({(f.get("source") or {}).get("provider", "") for f in figures.values()} - {""})
    credits = "重新排版版本（非原卷影印）。"
    if any(p.startswith("deekur") for p in providers):
        credits += "插图取自 deekur/gaokaomath 排版文件的矢量裁切，CC BY 4.0，https://github.com/deekur/gaokaomath ；"
    credits += "试题著作权归命题单位。"
    info = {"title": f"{paper.meta['title']} {paper.meta.get('subject', '')}".replace("$", "").strip(),
            "author": "", "subject": credits, "keywords": paper.meta["paper_id"],
            "creator": "MathAgent past-paper pipeline", "producer": "MathAgent past-paper pipeline"}
    info.update(metadata or {})
    doc.set_metadata(info)
    doc.save(pdf_path, garbage=3, deflate=True)
    doc.close()
    raw_pdf.unlink()

    doc = pymupdf.open(pdf_path)
    fonts = {}
    for page in doc:
        for font in page.get_fonts(full=True):
            fonts[font[3]] = font[1]
    not_embedded = sorted(name for name, ext in fonts.items() if ext in ("n/a", ""))
    page_sizes = sorted({(round(p.rect.width), round(p.rect.height)) for p in doc})
    stray_marks = margin_marks(doc)
    for index, page in enumerate(doc, start=1):
        page.get_pixmap(dpi=PREVIEW_DPI).save(out_dir / "preview" / f"page-{index}.png")
        page.get_pixmap(dpi=CHECK_DPI).save(out_dir / "check" / f"page-{index}.png")
    page_count = doc.page_count
    doc.close()

    blocking = []
    if probe["errors"]:
        blocking.append({"kind": "katex_error", "detail": probe["errors"]})
    for issue in probe["issues"]:
        blocking.append(issue)
    if not_embedded:
        blocking.append({"kind": "font_not_embedded", "fonts": not_embedded})
    # Chrome's A4 is 595.92 x 842.88 pt; anything else means a wrong paper size.
    if any(abs(w - 595) > 2 or abs(h - 842) > 2 for w, h in page_sizes):
        blocking.append({"kind": "page_size", "sizes": page_sizes})
    if stray_marks:
        blocking.append({"kind": "margin_ink", "marks": stray_marks})
    # A figure edited after it was cut no longer matches its recorded provenance.
    # figures.cut hashes the SVG text before write_text, so compare decoded text
    # (Windows newline translation changes the bytes on disk, not the text).
    for figure_id in paper.figure_ids():
        entry = figures.get(figure_id) or {}
        svg_path = paper_dir / entry.get("file", "")
        expected = entry.get("svg_sha256")
        if not svg_path.is_file() or not expected or hashlib.sha256(
                svg_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() != expected:
            blocking.append({"kind": "figure_provenance", "figure": figure_id})
    numbers = [q.number for q in paper.questions()]
    report = {
        "paper_id": paper.meta["paper_id"],
        "kind": kind,
        "source_sha256": sha256_file(source_path),
        "figures_sha256": sha256_file(figures_path) if figures_path.exists() else None,
        "pdf": pdf_path.name,
        "pdf_sha256": sha256_file(pdf_path),
        "pdf_bytes": pdf_path.stat().st_size,
        "pages": page_count,
        "page_size_pt": page_sizes,
        "question_numbers": numbers,
        "question_count": len(numbers),
        "figure_ids": paper.figure_ids(),
        "fonts": fonts,
        "blocking": blocking,
    }
    (out_dir / "render_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paper-dir", required=True, type=Path)
    parser.add_argument("--katex-dir", required=True, type=Path)
    parser.add_argument("--kind", default="student", choices=("student", "answer"))
    args = parser.parse_args(argv)
    label = "数学试题" if args.kind == "student" else "数学参考答案"
    report = render(args.paper_dir, args.katex_dir, kind=args.kind, footer_label=label)
    print(json.dumps({k: report[k] for k in ("paper_id", "kind", "pages", "question_count", "blocking")},
                     ensure_ascii=False))
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
