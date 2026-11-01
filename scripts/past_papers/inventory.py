"""Inventory collected Gaokao math source files into candidate exam identities.

Tooling only.  Scans the given raw directories, hashes every PDF, copies it
into a content-addressed, read-only originals store (``<sha256>.pdf``; the
first copy wins, so re-runs are idempotent) and parses the deekur/gaokaomath
naming scheme into a canonical paper id.  Region lists embedded in a file name
are recorded as that source's *claim* only; the year-specific region mapping
is maintained separately with its own evidence.

    python -m scripts.past_papers.inventory --workspace <dir> --raw <dir> [--raw <dir> ...] \
        [--sources-json <worker sources.json> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.past_papers import REGION_CODES, paper_id_for  # noqa: E402

TRACKS = {"文": "liberal", "理": "science"}
NUMERALS = {"1": "1", "2": "2", "3": "3"}

_PATTERNS = (
    # (regex on the name without year, series_key builder)
    (re.compile(r"^(?:春季上海|上海春季)$"), lambda m, y: ("shanghai", "spring", None)),
    (re.compile(r"^全国([123])([文理])$"), lambda m, y: (f"national_{m[1]}", "summer", TRACKS[m[2]])),
    (re.compile(r"^全国([甲乙])([文理])$"), lambda m, y: ({"甲": "national_jia", "乙": "national_yi"}[m[1]], "summer", TRACKS[m[2]])),
    (re.compile(r"^全国([文理])$"), lambda m, y: ("national_jia", "summer", TRACKS[m[1]]) if y == 2024 else None),
    (re.compile(r"^全国([12])$"), lambda m, y: (f"national_{m[1]}_2025", "summer", None) if y >= 2025 else None),
    (re.compile(r"^新高考([12])$"), lambda m, y: (f"new_curriculum_{m[1]}", "summer", None)),
    (re.compile(r"^新课标([12])?([文理])$"), lambda m, y: (f"legacy_xinkebiao{('_' + m[1]) if m[1] else ''}", "summer", TRACKS[m[2]])),
    (re.compile(r"^大纲([12])?([文理])$"), lambda m, y: (f"legacy_dagang{('_' + m[1]) if m[1] else ''}", "summer", TRACKS[m[2]])),
    (re.compile(r"^(\w{2})([文理])?$"), lambda m, y: (REGION_CODES[m[1]], "summer", TRACKS.get(m[2])) if m[1] in REGION_CODES else None),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_deekur_name(filename: str):
    """Return (year, series_key, session, track, claimed_regions) or None."""
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    match = re.match(r"^(\d{4})(.*?)(?:\((.*)\))?$", stem)
    if not match:
        return None
    year = int(match.group(1))
    name = match.group(2).strip()
    regions = [r.strip(" .") for r in re.split(r"[,，.]", match.group(3) or "") if r.strip(" .")]
    for pattern, build in _PATTERNS:
        found = pattern.match(name)
        if found:
            parsed = build(found, year)
            if parsed:
                series_key, session, track = parsed
                return year, series_key, session, track, regions
    return None


def store_original(path: Path, originals: Path) -> tuple[str, Path]:
    digest = sha256_file(path)
    target = originals / f"{digest}{path.suffix.lower()}"
    if not target.exists():
        originals.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    elif sha256_file(target) != digest:
        raise SystemExit(f"originals store corrupted: {target}")
    return digest, target


def load_source_urls(paths) -> dict:
    """Map sha256 -> source record from earlier collection workers."""
    out = {}
    for path in paths or []:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("sources", [])
        for item in items:
            if item.get("sha256"):
                out[item["sha256"]] = {k: item.get(k) for k in ("url", "commit", "repository_path", "license_or_rights")}
    return out


def located_from_tree(tree_path: Path, base_url: str) -> dict:
    """Papers listed in a source repository tree (located, not necessarily acquired)."""
    from urllib.parse import quote

    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    located = {}
    for item in tree.get("tree", tree) if isinstance(tree, dict) else tree:
        path = item.get("path", "")
        if not path.endswith(".pdf"):
            continue
        parsed = parse_deekur_name(path.rsplit("/", 1)[-1])
        if not parsed:
            continue
        year, series_key, session, track, regions = parsed
        paper_id = paper_id_for(year, series_key, session, track)
        located.setdefault(paper_id, {"paper_id": paper_id, "year": year, "session": session,
                                      "series_key": series_key, "track": track, "urls": [],
                                      "claimed_regions": regions})
        located[paper_id]["urls"].append(base_url.rstrip("/") + "/" + quote(path))
    return dict(sorted(located.items()))


def run(workspace: Path, raw_dirs, sources_json=None, tree=None, tree_base_url=None) -> dict:
    from urllib.parse import quote

    originals = workspace / "originals"
    known = load_source_urls(sources_json)
    located = located_from_tree(tree, tree_base_url or "") if tree else {}
    files = []
    candidates: dict[str, dict] = {}
    for raw in raw_dirs:
        for path in sorted(Path(raw).rglob("*.pdf")):
            digest, stored = store_original(path, originals)
            record = {"sha256": digest, "stored": stored.relative_to(workspace).as_posix(),
                      "found_at": str(path), "bytes": path.stat().st_size, **known.get(digest, {})}
            # deekur's layout: <普通高考|春季高考>/<year>/<year><paper>(<regions>).pdf
            in_deekur_layout = path.parent.parent.name in ("普通高考", "春季高考")
            parsed = parse_deekur_name(path.name) if in_deekur_layout or "deekur" in (record.get("url") or "") else None
            if parsed:
                year, series_key, session, track, regions = parsed
                paper_id = paper_id_for(year, series_key, session, track)
                if not record.get("url"):
                    # Provenance from the pinned repository tree when no worker record has it.
                    suffix = "/" + quote(path.name)
                    record["url"] = next((u for u in located.get(paper_id, {}).get("urls", []) if u.endswith(suffix)), None)
                record.update({"paper_id": paper_id, "lineage": "deekur/gaokaomath", "role": "student_typeset",
                               "claimed_regions": regions})
                entry = candidates.setdefault(paper_id, {
                    "paper_id": paper_id, "year": year, "session": session, "series_key": series_key,
                    "track": track, "files": [], "claimed_regions": {}})
                if digest not in [f["sha256"] for f in entry["files"]]:
                    entry["files"].append({"sha256": digest, "stored": record["stored"], "lineage": "deekur/gaokaomath",
                                           "url": record.get("url")})
                entry["claimed_regions"]["deekur/gaokaomath"] = regions
            else:
                record["paper_id"] = None
            files.append(record)
    out_dir = workspace / "inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    unique = {f["sha256"] for f in files}
    summary = {"files_seen": len(files), "unique_files": len(unique), "candidate_papers": len(candidates)}
    if tree:
        (out_dir / "located.json").write_text(json.dumps(located, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["located_papers"] = len(located)
        summary["located_not_acquired"] = len(set(located) - set(candidates))
    (out_dir / "files.json").write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "candidates.json").write_text(
        json.dumps(dict(sorted(candidates.items())), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--raw", required=True, action="append", type=Path)
    parser.add_argument("--sources-json", action="append")
    parser.add_argument("--tree", type=Path, help="GitHub tree JSON of a source repository (located papers)")
    parser.add_argument("--tree-base-url", help="raw URL prefix for --tree paths (pinned commit)")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace, args.raw, args.sources_json, args.tree, args.tree_base_url),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
