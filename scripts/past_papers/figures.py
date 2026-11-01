"""Extract exam diagrams from a source PDF as vector SVG crops.

Tooling only.  Diagrams are never redrawn: each figure is a clipped copy of the
source page (vector paths and label glyphs as outlines), and its provenance
(source SHA-256, 1-based page, crop rectangle in PDF points) is stored in the
paper's ``figures.json``.

    # list candidate regions (vector drawing clusters) with PNG previews
    python -m scripts.past_papers.figures candidates --pdf src.pdf --out <dir>
    # cut one figure and register it in <paper-dir>/figures.json
    python -m scripts.past_papers.figures cut --pdf src.pdf --page 1 \
        --rect 540 100 700 250 --paper-dir <dir> --id q11-1 --width-mm 38
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merge(rects, gap):
    import pymupdf

    # Axis lines have zero height or width; an empty Rect never "intersects".
    boxes = [pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5) for r in map(pymupdf.Rect, rects)]
    changed = True
    while changed:
        changed = False
        out = []
        while boxes:
            box = boxes.pop()
            grown = pymupdf.Rect(box.x0 - gap, box.y0 - gap, box.x1 + gap, box.y1 + gap)
            for other in list(boxes):
                if grown.intersects(other):
                    box |= other
                    boxes.remove(other)
                    changed = True
            out.append(box)
        boxes = out
    return boxes


def candidates(pdf: Path, out_dir: Path, *, min_size: float = 18.0) -> list:
    """Cluster vector drawings and images into figure-sized regions."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    found = []
    for pno, page in enumerate(doc):
        # Isolated fraction bars and radical overbars form clusters thinner than
        # ``min_size`` and are dropped below; axes stay attached to their figure.
        rects = [drawing["rect"] for drawing in page.get_drawings()]
        for info in page.get_image_info():
            rects.append(pymupdf.Rect(info["bbox"]))
        for box in _merge(rects, 6):
            if box.width < min_size or box.height < min_size:
                continue
            # Include short labels (O, x, A1 ...) that sit around the drawing.
            grown = pymupdf.Rect(box.x0 - 14, box.y0 - 12, box.x1 + 14, box.y1 + 12)
            for x0, y0, x1, y1, text, *_ in page.get_text("words"):
                word = pymupdf.Rect(x0, y0, x1, y1)
                if grown.intersects(word) and len(text) <= 4:
                    box |= word
            box = pymupdf.Rect(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2) & page.rect
            index = len(found) + 1
            png = out_dir / f"cand-{index:02d}-p{pno + 1}.png"
            page.get_pixmap(dpi=200, clip=box).save(png)
            found.append({"index": index, "page": pno + 1,
                          "rect": [round(v, 1) for v in box], "preview": png.name})
    (out_dir / "candidates.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def cut(pdf: Path, page_no: int, rect, paper_dir: Path, figure_id: str, *, width_mm: float,
        provider: str, note: str = "") -> dict:
    import pymupdf

    src = pymupdf.open(pdf)
    clip = pymupdf.Rect(rect)
    if not clip.is_valid or clip.is_empty or clip not in src[page_no - 1].rect:
        raise SystemExit("crop rectangle must lie inside the page")
    # Work on a scratch copy of the page: drop everything outside the crop so
    # the SVG carries only the diagram's own paths and labels, then crop.
    tmp = pymupdf.open()
    tmp.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
    target = tmp[0]
    outer = target.rect
    keep = pymupdf.Rect(clip.x0 - 3, clip.y0 - 3, clip.x1 + 3, clip.y1 + 3) & outer
    for band in (
        pymupdf.Rect(outer.x0, outer.y0, outer.x1, keep.y0),
        pymupdf.Rect(outer.x0, keep.y1, outer.x1, outer.y1),
        pymupdf.Rect(outer.x0, keep.y0, keep.x0, keep.y1),
        pymupdf.Rect(keep.x1, keep.y0, outer.x1, keep.y1),
    ):
        if band.width > 0 and band.height > 0:
            target.add_redact_annot(band)
    target.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
    target.set_cropbox(clip)
    svg = target.get_svg_image(text_as_path=True)
    fig_dir = paper_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    svg_path = fig_dir / f"{figure_id}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    target.get_pixmap(dpi=300).save(fig_dir / f"{figure_id}.check.png")
    registry_path = paper_dir / "figures.json"
    registry = {"figures": {}}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = {
        "file": f"figures/{figure_id}.svg",
        "width_mm": width_mm,
        "vector": True,
        "source": {"provider": provider, "sha256": _sha256(pdf), "page": page_no,
                   "rect_pt": [round(v, 2) for v in clip]},
        "svg_sha256": hashlib.sha256(svg.encode("utf-8")).hexdigest(),
        "note": note,
    }
    registry["figures"][figure_id] = entry
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract source diagrams as vector crops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cand = sub.add_parser("candidates")
    cand.add_argument("--pdf", required=True, type=Path)
    cand.add_argument("--out", required=True, type=Path)
    one = sub.add_parser("cut")
    one.add_argument("--pdf", required=True, type=Path)
    one.add_argument("--page", required=True, type=int)
    one.add_argument("--rect", required=True, nargs=4, type=float)
    one.add_argument("--paper-dir", required=True, type=Path)
    one.add_argument("--id", required=True)
    one.add_argument("--width-mm", type=float, default=40.0)
    one.add_argument("--provider", default="deekur/gaokaomath")
    one.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.cmd == "candidates":
        print(json.dumps(candidates(args.pdf, args.out), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(cut(args.pdf, args.page, args.rect, args.paper_dir, args.id,
                             width_mm=args.width_mm, provider=args.provider, note=args.note),
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
