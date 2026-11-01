"""Release checks on the exact files a teacher downloads from a built library.

Tooling only (needs pymupdf).  For every catalog file:

* the bytes match the catalog SHA-256 and the PDF opens;
* every page is A4 portrait and every font is embedded;
* the student paper's text contains every question number of the verified
  transcription, in order, and none of the answer/solution markers;
* no page carries advertising, platform branding, URLs or QR-code prompts.

    python -m scripts.past_papers.release_check --library <dir> --workspace <ws> [--out report.json]

Exit status 1 if any file fails; the JSON report lists every finding.
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

from scripts.past_papers.paper_format import parse_paper  # noqa: E402

# Only unambiguous markers: "解析式", "评分" (judges' scores) or "把答案填在横线上"
# legitimately occur in questions and instructions; visual review covers the rest.
ANSWER_MARKERS = ("【答案】", "答案：", "参考答案", "【解析】", "解析：", "【详解】", "【分析】", "【解答】",
                  "【点睛】", "故选", "故答案为")
AD_MARKERS = ("公众号", "扫码", "二维码", "QQ群", "学科网", "组卷网", "菁优", "锦宏", "http", "www.")


def _text(doc) -> str:
    return "\n".join(page.get_text() for page in doc)


def check_pdf(path: Path, expected_sha: str, *, kind: str, question_numbers=None) -> list:
    import pymupdf

    problems = []
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha:
        return ["checksum differs from catalog"]
    doc = pymupdf.open(stream=data, filetype="pdf")
    for index, page in enumerate(doc, start=1):
        width, height = page.rect.width, page.rect.height
        if abs(width - 595) > 3 or abs(height - 842) > 3:
            problems.append(f"page {index} is {width:.0f}x{height:.0f}pt, not A4 portrait")
        for font in page.get_fonts(full=True):
            if font[1] in ("n/a", ""):
                problems.append(f"page {index}: font {font[3]} not embedded")
    text = _text(doc)
    compact = re.sub(r"\s+", "", text)
    for marker in AD_MARKERS:
        if marker.lower() in compact.lower():
            problems.append(f"advertising/branding marker {marker!r}")
    if kind == "student":
        for marker in ANSWER_MARKERS:
            if marker in compact:
                problems.append(f"answer/solution marker {marker!r} in student paper")
        position = 0
        for number in question_numbers or []:
            match = re.search(rf"(?<![0-9]){re.escape(number)}\.", text[position:])
            if not match:
                problems.append(f"question {number} not found in order")
                break
            position += match.end()
    return problems


def run(library: Path, workspace: Path) -> dict:
    catalog = json.loads((library / "catalog.json").read_text(encoding="utf-8"))
    report = {"papers": [], "failures": 0}
    for paper in catalog["papers"]:
        paper_id = paper["paper_id"]
        source = workspace / "papers" / paper_id / "student.paper.txt"
        numbers = [q.number for q in parse_paper(source.read_text(encoding="utf-8")).questions()]
        entry = {"paper_id": paper_id, "question_count": len(numbers), "files": {}}
        for kind, info in paper["files"].items():
            problems = check_pdf(library / info["path"], info["sha256"], kind=kind,
                                 question_numbers=numbers if kind == "student" else None)
            entry["files"][kind] = {"pages": info["pages"], "problems": problems}
            report["failures"] += bool(problems)
        report["papers"].append(entry)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = run(args.library, args.workspace)
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for paper in report["papers"]:
        for kind, result in paper["files"].items():
            status = "OK" if not result["problems"] else "FAIL " + "; ".join(result["problems"])
            print(f"{paper['paper_id']} {kind} pages={result['pages']} {status}")
    print(f"{len(report['papers'])} paper(s), {report['failures']} failing file(s)")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
