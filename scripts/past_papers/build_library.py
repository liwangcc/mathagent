"""Build the teacher-facing 整卷真题 library from verified paper work directories.

Pure Python (no PDF libraries): it only checks records and copies files.

    python -m scripts.past_papers.build_library --workspace <ws> --out <library-dir> \
        [--regions references/past_papers/region_usage.json] [--rights <operator decisions.json>]

Gate per paper (everything else is reported in the coverage manifest only):

* ``verification.json`` status ``verified`` and an independent ``review.json``
  with ``status: pass`` that names the same transcription SHA-256;
* the student render report has no blocking issues and its PDF/transcription
  hashes match the files on disk (a stale PDF is never published);
* an answer PDF is attached only when the answer key itself is verified,
  reviewed and rendered without blocking issues.

Rights are never inferred: every paper is ``unresolved`` unless an
operator-owned decisions file grants a scoped decision.  The web app applies
the scope gate again at request time.

Idempotent and resumable: files are content-addressed
(``files/<paper_id>/student-<sha12>.pdf``), ``catalog.json`` is replaced
atomically after all files are in place, and unreferenced files are pruned
afterwards.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.past_papers import (  # noqa: E402
    REGION_NAMES, SCHEMA_VERSION, SERIES, TRACK_NAMES, paper_id_for, series_name, validate_catalog,
)

DEEKUR_ATTRIBUTION = {
    "work": "deekur/gaokaomath（高考数学试题 LaTeX 排版合集）",
    "url": "https://github.com/deekur/gaokaomath",
    "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "use": "试卷插图为该仓库排版 PDF 的矢量裁切（仅缩放，未改动图形内容）；该许可只覆盖其排版整理，不覆盖试题本身。",
}
EDITION = {
    "kind": "retypeset",
    "label": "忠实重排版",
    "layout": "A4",
    "description": ("依据多份相互独立的流传版本逐题核对后重新排版（A4 单栏），"
                    "非考试院原卷影印；题目文字、数据、选项、小问与分值以多数一致的来源为准，"
                    "核对记录可追溯。"),
}
RIGHTS_NOTICE = ("高考试题著作权归命题单位所有。本资源仅供已登录教师用于课堂教学与练习，"
                 "请勿公开传播或商业使用。")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def parse_paper_id(paper_id: str, candidates: dict) -> dict | None:
    entry = candidates.get(paper_id)
    if not entry:
        return None
    return {k: entry[k] for k in ("year", "session", "series_key", "track")}


def region_lookup(regions_doc: dict | None, year: int, series_key: str) -> tuple[list, str]:
    if SERIES[series_key]["kind"] == "regional":
        return [series_key], "self_set"
    year_doc = ((regions_doc or {}).get("years") or {}).get(str(year)) or {}
    mapping = year_doc.get(series_key)
    if not mapping:
        return [], "unmapped"
    regions = [r for r in mapping.get("regions", []) if r in REGION_NAMES]
    return regions, mapping.get("confidence", "unknown")


def check_render(build_dir: Path, source_path: Path) -> tuple[dict | None, list]:
    """Return (render_report, problems) for one kind (student/answer)."""
    problems = []
    report = _load(build_dir / "render_report.json")
    if report is None:
        return None, ["no render report"]
    if report.get("blocking"):
        problems.append(f"render blocking issues: {report['blocking']}")
    pdf = build_dir / report.get("pdf", "")
    if not pdf.is_file() or sha256_file(pdf) != report.get("pdf_sha256"):
        problems.append("rendered PDF missing or differs from its report")
    if not source_path.is_file() or sha256_file(source_path) != report.get("source_sha256"):
        problems.append("transcription changed after rendering (stale PDF)")
    previews = sorted((build_dir / "preview").glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
    if len(previews) != report.get("pages"):
        problems.append("preview pages do not match PDF pages")
    return report, problems


def review_problem(review: dict | None, verification: dict, report: dict | None) -> str | None:
    """Why a review cannot clear the student paper, or None when it does.

    The review must pass, come from someone other than the verifier, and name
    the exact transcription and figure set that were rendered: re-cutting a
    figure or editing the text after review invalidates it.
    """
    if not review or review.get("status") != "pass":
        return "independent review missing or not passed"
    reviewer = str(review.get("reviewer") or "").strip()
    if not reviewer or reviewer == str(verification.get("verifier") or "").strip():
        return "review must name a reviewer other than the verifier"
    if report and review.get("student_source_sha256") != report.get("source_sha256"):
        return "review was for a different transcription"
    if report and review.get("student_figures_sha256") != report.get("figures_sha256"):
        return "review was for a different figure set"
    return None


def evaluate_paper(paper_dir: Path, candidates: dict, *, require_review: bool = True) -> dict:
    """Assess one work directory; returns a record with status and reasons."""
    paper_id = paper_dir.name
    identity = parse_paper_id(paper_id, candidates)
    record = {"paper_id": paper_id, "identity": identity, "publishable": False, "reasons": [],
              "answer_publishable": False, "answer_reasons": []}
    if identity is None:
        record["reasons"].append("not a known candidate id")
        return record
    verification = _load(paper_dir / "verification.json")
    record["verification"] = verification
    if not verification:
        record["reasons"].append("no verification record")
        return record
    if verification.get("status") != "verified":
        record["reasons"].append("verification status: " + str(verification.get("status")))
        # Workers sometimes record structured reasons ({"q": ..., "reason": ...}).
        record["reasons"].extend(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
                                 for r in verification.get("quarantine_reasons") or [])
        return record
    student_src = paper_dir / "student.paper.txt"
    report, problems = check_render(paper_dir / "build" / "student", student_src)
    record["render"] = report
    record["reasons"].extend(problems)
    review = _load(paper_dir / "review.json")
    record["review"] = review
    if require_review:
        problem = review_problem(review, verification, report)
        if problem:
            record["reasons"].append(problem)
    record["publishable"] = not record["reasons"]
    answers = verification.get("answers") or {}
    if answers.get("status") == "verified" and (paper_dir / "answer.paper.txt").is_file():
        a_report, a_problems = check_render(paper_dir / "build" / "answer", paper_dir / "answer.paper.txt")
        record["answer_render"] = a_report
        record["answer_reasons"].extend(a_problems)
        if require_review:
            if not review or review.get("answer_status") != "pass":
                record["answer_reasons"].append("answer key not independently reviewed")
            elif a_report and review.get("answer_source_sha256") != a_report.get("source_sha256"):
                record["answer_reasons"].append("answer review was for a different answer key")
        record["answer_publishable"] = record["publishable"] and not record["answer_reasons"]
    else:
        record["answer_reasons"].append("answer key status: " + str(answers.get("status", "unavailable")))
    return record


def _copy_content_addressed(src: Path, out: Path, rel_dir: str, stem: str) -> tuple[str, str]:
    digest = sha256_file(src)
    rel = f"{rel_dir}/{stem}-{digest[:12]}{src.suffix.lower()}"
    target = out / rel
    if not target.exists() or sha256_file(target) != digest:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    return rel, digest


def _file_entry(build_dir: Path, report: dict, out: Path, paper_id: str, kind: str) -> dict:
    pdf_rel, pdf_sha = _copy_content_addressed(build_dir / report["pdf"], out, f"files/{paper_id}", kind)
    previews = []
    for index in range(1, report["pages"] + 1):
        rel, _ = _copy_content_addressed(build_dir / "preview" / f"page-{index}.png", out,
                                         f"previews/{paper_id}", f"{kind}-{index}")
        previews.append(rel)
    return {"path": pdf_rel, "sha256": pdf_sha, "bytes": (out / pdf_rel).stat().st_size,
            "pages": report["pages"], "page_size": "A4", "previews": previews}


def _rights_for(paper_id: str, decisions_doc: dict | None) -> dict:
    for decision in (decisions_doc or {}).get("decisions", []):
        applies = decision.get("applies_to") or {}
        if paper_id in (applies.get("paper_ids") or []) or applies.get("all_verified") is True:
            return {
                "status": "operator_approved",
                "decision": {k: decision.get(k) for k in ("decided_by", "decided_at", "basis", "scope")},
                "notice": decision.get("notice") or RIGHTS_NOTICE,
            }
    return {"status": "unresolved", "decision": None, "notice": RIGHTS_NOTICE,
            "detail": "试题著作权归命题单位；所用排版来源的开放许可不覆盖试题本身，尚无运营方授权决定。"}


def _catalog_entry(record: dict, paper_dir: Path, out: Path, regions_doc, decisions_doc) -> dict:
    identity = record["identity"]
    verification = record["verification"]
    paper_id = record["paper_id"]
    year, series_key, track = identity["year"], identity["series_key"], identity["track"]
    regions, region_confidence = region_lookup(regions_doc, year, series_key)
    files = {"student": _file_entry(paper_dir / "build" / "student", record["render"], out, paper_id, "student")}
    answer = {"status": "unavailable", "kind": "", "note": "暂无经核验的答案文件。"}
    if record["answer_publishable"]:
        files["answer"] = _file_entry(paper_dir / "build" / "answer", record["answer_render"], out, paper_id,
                                      "answer")
        answer = {"status": "verified", "kind": "final_answers",
                  "note": "非官方参考答案：仅列最终答案，经多个独立来源核对。"}
    sources = _load(paper_dir / "sources.json", {}) or {}
    lineages = [l.get("id") for l in sources.get("lineages", []) if l.get("id")]
    deviations = sum(len(q.get("deviations") or []) for q in verification.get("questions") or [])
    figures = _load(paper_dir / "figures.json", {}) or {}
    attribution = []
    if any((f.get("source") or {}).get("provider", "").startswith("deekur") for f in figures.get("figures", {}).values()):
        attribution.append(DEEKUR_ATTRIBUTION)
    exam = verification.get("exam") or {}
    notes = ["本版本为重新排版的 A4 版，页码与原卷不同；省略了答题卡填涂注意事项。"]
    if region_confidence == "conflict":
        notes.append("适用地区中个别省份的归属存在来源冲突，已按最可信证据暂定。")
    elif region_confidence not in ("confirmed", "self_set"):
        notes.append("适用地区信息待进一步核实。")
    title = exam.get("title") or ""
    track_name = TRACK_NAMES.get(track, "")
    return {
        "paper_id": paper_id,
        "year": year,
        "session": identity["session"],
        "series_key": series_key,
        "track": track,
        "title": f"{title}{(' ' + track_name + '数学') if track_name else ' 数学'}".strip(),
        "regions": regions,
        "region_confidence": region_confidence,
        "question_count": exam.get("question_count"),
        "total_points": exam.get("total_points"),
        "edition": EDITION,
        "files": files,
        "answer": answer,
        "verification": {
            "status": "verified",
            "method": "multi_lineage_majority_with_independent_review",
            "lineages": lineages,
            "summary": f"{len(lineages)} 个来源版本逐题核对，记录并纠正来源间差异 {deviations} 处；另经独立复核。",
            "reviewed_at": (record.get("review") or {}).get("reviewed_at"),
        },
        "rights": _rights_for(paper_id, decisions_doc),
        "attribution": attribution,
        "notes": notes,
    }


def coverage_rows(candidates: dict, records: dict, regions_doc, located: dict | None = None,
                  exclusions: dict | None = None) -> list:
    rows = []
    exclusions = exclusions or {}
    for paper_id, entry in sorted((located or {}).items()):
        if paper_id in candidates:
            continue
        rows.append({
            "paper_id": paper_id, "year": entry["year"], "session": entry["session"],
            "series": series_name(entry["series_key"], entry["year"]),
            "track": TRACK_NAMES.get(entry["track"], ""),
            "source_located": True, "file_acquired": False, "independent_lineages": 0,
            "student_verified": False, "print_ready": False, "answer_verified": False,
            "rights": "unresolved", "regions_mapping": region_lookup(regions_doc, entry["year"], entry["series_key"])[1],
            "status": "located_not_acquired", "reasons": "outside this run's collection scope",
        })
    for paper_id, cand in sorted(candidates.items()):
        rec = records.get(paper_id)
        verification = (rec or {}).get("verification") or {}
        lineages = []
        if rec:
            sources = _load(Path(rec["dir"]) / "sources.json", {}) or {}
            lineages = [l.get("id") for l in sources.get("lineages", [])]
        regions, confidence = region_lookup(regions_doc, cand["year"], cand["series_key"])
        rows.append({
            "paper_id": paper_id,
            "year": cand["year"],
            "session": cand["session"],
            "series": series_name(cand["series_key"], cand["year"]),
            "track": TRACK_NAMES.get(cand["track"], ""),
            "source_located": True,
            "file_acquired": bool(cand.get("files")),
            "independent_lineages": max(len(lineages) - 1, 0) if lineages else 0,
            "student_verified": verification.get("status") == "verified",
            "print_ready": bool(rec and rec.get("publishable")),
            "answer_verified": bool(rec and rec.get("answer_publishable")),
            "rights": (rec or {}).get("rights_status", "unresolved"),
            "regions_mapping": confidence,
            "status": ("excluded" if paper_id in exclusions else
                       "publishable" if rec and rec.get("publishable") else
                       verification.get("status") or ("in_progress" if rec else "not_started")),
            "reasons": exclusions.get(paper_id) or "; ".join((rec or {}).get("reasons") or []),
        })
    return rows


def build(workspace: Path, out: Path, *, regions_path: Path | None = None, rights_path: Path | None = None,
          require_review: bool = True) -> dict:
    candidates = _load(workspace / "inventory" / "candidates.json", {})
    regions_doc = _load(regions_path) if regions_path else None
    decisions_doc = _load(rights_path) if rights_path else None
    # Documented scope exclusions (e.g. papers never officially released) never publish.
    exclusions = _load(workspace / "inventory" / "exclusions.json", {}) or {}
    out.mkdir(parents=True, exist_ok=True)
    records = {}
    entries = []
    for paper_dir in sorted((workspace / "papers").iterdir()):
        if not paper_dir.is_dir():
            continue
        record = evaluate_paper(paper_dir, candidates, require_review=require_review)
        record["dir"] = str(paper_dir)
        records[record["paper_id"]] = record
        if record["paper_id"] in exclusions:
            record["publishable"] = record["answer_publishable"] = False
            record["reasons"].insert(0, "excluded: " + exclusions[record["paper_id"]])
        if record["publishable"]:
            identity = record["identity"]
            assert record["paper_id"] == paper_id_for(identity["year"], identity["series_key"],
                                                      identity["session"], identity["track"])
            entry = _catalog_entry(record, paper_dir, out, regions_doc, decisions_doc)
            record["rights_status"] = entry["rights"]["status"]
            entries.append(entry)
    catalog = {"schema_version": SCHEMA_VERSION,
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "papers": entries}
    validate_catalog(catalog)
    tmp = out / "catalog.json.tmp"
    tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out / "catalog.json")
    referenced = {e["files"][k]["path"] for e in entries for k in e["files"]}
    referenced |= {p for e in entries for k in e["files"] for p in e["files"][k]["previews"]}
    pruned = 0
    for folder in ("files", "previews"):
        for path in (out / folder).rglob("*") if (out / folder).exists() else []:
            if path.is_file() and path.relative_to(out).as_posix() not in referenced:
                path.unlink()
                pruned += 1
    rows = coverage_rows(candidates, records, regions_doc, _load(workspace / "inventory" / "located.json", {}),
                         exclusions)
    rows.sort(key=lambda r: (-r["year"], r["paper_id"]))
    cov_dir = workspace / "coverage"
    cov_dir.mkdir(parents=True, exist_ok=True)
    (cov_dir / "coverage.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (cov_dir / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["paper_id"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "candidates": len(candidates),
        "work_dirs": len(records),
        "published_papers": len(entries),
        "published_answers": sum(1 for e in entries if "answer" in e["files"]),
        "rights_approved": sum(1 for e in entries if e["rights"]["status"] != "unresolved"),
        "pruned_files": pruned,
    }
    (cov_dir / "build_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--regions", type=Path)
    parser.add_argument("--rights", type=Path)
    parser.add_argument("--no-review-required", action="store_true",
                        help="testing only: skip the independent review gate")
    args = parser.parse_args(argv)
    summary = build(args.workspace, args.out, regions_path=args.regions, rights_path=args.rights,
                    require_review=not args.no_review_required)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
