"""Cross-check a transcription against a second lineage's PDF text layer.

Tooling only.  For every question this compares multisets of CJK characters,
digits and Latin letters between the ``.paper.txt`` transcription and the text
layer of an independent typeset PDF (deekur by default).  Formula layout makes
character *order* unreliable in PDF text, but the counts survive, so a missing
word, a changed number or a dropped variable shows up as a difference.  Each
difference is a flag for a person/agent to adjudicate against the sources; the
tool never decides which side is right.

    python -m scripts.past_papers.crosscheck --paper-dir <dir> --pdf <deekur.pdf>
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.past_papers.paper_format import parse_paper  # noqa: E402

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
FUNCTION_NAMES = ("arcsin", "arccos", "arctan", "sin", "cos", "tan", "cot", "sec", "csc", "ln", "lg",
                  "log", "lim", "max", "min", "exp", "det")
QUESTION_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[.．]\s*")
SECTION_LINE_RE = re.compile(r"^\s*[一二三四五六七八九十]+、")


def latex_plain(text: str) -> str:
    """Reduce LaTeX source to the characters a reader would see."""
    text = re.sub(r"\{\{fig:[^}]*\}\}", "", text)
    text = re.sub(r"\\(?:begin|end)\{[a-z*]+\}", " ", text)
    # Pad with spaces so "\cdot\boldsymbol{a}" cannot fuse into one command name.
    text = re.sub(r"\\(?:text|mathrm|textrm|mathbf|boldsymbol|mathit|operatorname)\s*\{([^{}]*)\}", r" \1 ", text)
    for name in FUNCTION_NAMES:
        text = text.replace("\\" + name, " " + name + " ")
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    return text


def features(text: str) -> dict:
    return {
        "cjk": Counter(CJK_RE.findall(text)),
        "digits": Counter(re.findall(r"\d", text)),
        "letters": Counter(re.findall(r"[A-Za-z]", text)),
    }


def transcription_questions(paper) -> dict:
    out = {}
    for q in paper.questions():
        # Per-question scores such as "（13分）" are absent from most typesets.
        stem = re.sub(r"^\s*[（(](?:本小题满分|本题满分|本小题|满分)?\s*\d+\s*分[）)]\s*", "", q.stem)
        parts = [stem] + [text for _, text in q.choices]
        for block in q.blocks:
            parts.append(block.text)
            for row in block.rows:
                parts.extend(row)
        out[q.number] = latex_plain(" ".join(parts))
    return out


def _figure_boxes(page):
    import pymupdf
    from scripts.past_papers.figures import _merge

    boxes = _merge([d["rect"] for d in page.get_drawings()], 6)
    return [pymupdf.Rect(b.x0 - 12, b.y0 - 12, b.x1 + 12, b.y1 + 12)
            for b in boxes if b.width >= 18 and b.height >= 18]


def pdf_questions(pdf: Path) -> dict:
    """Split a typeset PDF's text layer into questions.

    Lines are read column by column.  A question owns the band of its column
    from just above its number line down to the next number line, so the
    raised numerator of a tall formula stays with its own question.  Short
    labels inside diagrams (O, x, A1 ...) are left out.
    """
    import pymupdf

    doc = pymupdf.open(pdf)
    columns = []  # [(lines sorted by y)] in reading order
    for page in doc:
        width = page.rect.width
        bounds = [0, width * 0.345, width * 0.657, width + 1] if width > 1000 else [0, width + 1]
        figures = _figure_boxes(page)
        per_column = [[] for _ in range(len(bounds) - 1)]
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                rect = pymupdf.Rect(line["bbox"])
                text = "".join(span["text"] for span in line["spans"])
                if not text.strip():
                    continue
                # Book page numbers sit alone in the bottom margin.
                if rect.y0 > page.rect.height - 45 and re.fullmatch(r"\s*\d+\s*", text):
                    continue
                if len(text.strip()) <= 3 and any(rect in box for box in figures):
                    continue
                column = next(i for i in range(len(bounds) - 1) if bounds[i] <= rect.x0 < bounds[i + 1])
                per_column[column].append((rect.y0, rect.y1, rect.x0, text))
        columns.extend(sorted(lines) for lines in per_column)
    questions = {}
    current = None
    expected = 1
    for lines in columns:
        anchors = []
        probe = expected
        for y0, _, _, text in lines:
            match = QUESTION_LINE_RE.match(text)
            if match and int(match.group(1)) == probe:
                anchors.append((y0 - 9, match.group(1)))
                probe += 1
        sections = [y0 - 3 for y0, _, _, text in lines if SECTION_LINE_RE.match(text)]
        for y0, y1, _, text in lines:
            centre = (y0 + y1) / 2
            owner = current
            for start, number in anchors:
                if centre >= start:
                    owner = number
            # Section headings end the previous question.
            if any(start <= centre < start + 14 for start in sections) or (
                    owner == current and any(s <= centre for s in sections)
                    and not any(a[0] <= centre for a in anchors)):
                continue
            match = QUESTION_LINE_RE.match(text)
            if match and owner == match.group(1):
                text = text[match.end():]
            if owner is not None:
                questions[owner] = questions.get(owner, "") + " " + text
        if anchors:
            current = anchors[-1][1]
            expected = int(current) + 1
    for key, value in questions.items():
        questions[key] = re.sub(r"\(([A-H])\)", " ", value)
    return questions


def diff(ours: Counter, theirs: Counter) -> dict:
    missing = theirs - ours
    extra = ours - theirs
    return {"only_in_other": "".join(sorted(missing.elements())), "only_in_ours": "".join(sorted(extra.elements()))}


def crosscheck(paper_dir: Path, pdf: Path, *, label: str = "deekur") -> dict:
    paper = parse_paper((paper_dir / "student.paper.txt").read_text(encoding="utf-8"))
    ours = transcription_questions(paper)
    theirs = pdf_questions(pdf)
    report = {"paper_id": paper.meta["paper_id"], "other": label, "other_pdf": str(pdf),
              "questions_ours": list(ours), "questions_other": list(theirs), "flags": []}
    for number in sorted(set(ours) | set(theirs), key=lambda n: (int(re.match(r"\d+", n).group()), n)):
        if number not in ours or number not in theirs:
            report["flags"].append({"q": number, "kind": "missing_question",
                                    "in_ours": number in ours, "in_other": number in theirs})
            continue
        a, b = features(ours[number]), features(theirs[number])
        for key in ("cjk", "digits", "letters"):
            result = diff(a[key], b[key])
            if result["only_in_other"] or result["only_in_ours"]:
                report["flags"].append({"q": number, "kind": key, **result})
    out = paper_dir / f"crosscheck.{label}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paper-dir", required=True, type=Path)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--label", default="deekur")
    args = parser.parse_args(argv)
    report = crosscheck(args.paper_dir, args.pdf, label=args.label)
    for flag in report["flags"]:
        print(json.dumps(flag, ensure_ascii=False))
    print(f"{len(report['flags'])} flag(s); questions ours={len(report['questions_ours'])} "
          f"other={len(report['questions_other'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
