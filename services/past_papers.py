"""整卷真题：完整高考数学试卷资源库（目录、版权闸门与安全文件访问）。

The library is reference content, not teacher data: an offline ingestion
pipeline (``scripts/past_papers/``) verifies each paper and writes a
``catalog.json`` plus the PDF/preview files into a library directory.  The web
app only reads that directory; no request ever collects, converts or OCRs.

Separation of states is enforced here, not trusted to the pipeline:

* a paper is listed only if legacy content verification or the explicitly
  narrower purchased-PDF resource checks pass, and its rights gate passes;
* candidates, quarantined papers and rights-pending papers stay out of the
  teacher list (admins see only their counts);
* files are addressed by catalog identity (paper id + file kind), resolved
  inside the library root and checked against the catalog SHA-256 before they
  are served, so no client-supplied path ever reaches the filesystem.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading

SCHEMA_VERSION = 1
FILE_KINDS = ("student", "answer")
PAPER_ID_RE = re.compile(r"^gk\d{4}-[a-z0-9]+(?:-[a-z0-9]+){0,3}$")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){0,4}$")

# ---------------------------------------------------------------- vocabulary

REGIONS = (
    ("beijing", "北京"), ("tianjin", "天津"), ("hebei", "河北"), ("shanxi", "山西"),
    ("neimenggu", "内蒙古"), ("liaoning", "辽宁"), ("jilin", "吉林"), ("heilongjiang", "黑龙江"),
    ("shanghai", "上海"), ("jiangsu", "江苏"), ("zhejiang", "浙江"), ("anhui", "安徽"),
    ("fujian", "福建"), ("jiangxi", "江西"), ("shandong", "山东"), ("henan", "河南"),
    ("hubei", "湖北"), ("hunan", "湖南"), ("guangdong", "广东"), ("guangxi", "广西"),
    ("hainan", "海南"), ("chongqing", "重庆"), ("sichuan", "四川"), ("guizhou", "贵州"),
    ("yunnan", "云南"), ("xizang", "西藏"), ("shaanxi", "陕西"), ("gansu", "甘肃"),
    ("qinghai", "青海"), ("ningxia", "宁夏"), ("xinjiang", "新疆"),
)
REGION_NAMES = dict(REGIONS)
REGION_CODES = {name: key for key, name in REGIONS}

# Series are never merged by name alone: 新课标Ⅰ卷 (2013–2014, old curriculum)
# and 新课标Ⅰ卷 (2023–2024, new Gaokao) are different series with different keys.
# ``names`` maps inclusive year ranges to the name printed in that year.
SERIES = {
    "national_1": {"short": "n1", "kind": "national", "names": ((2015, 2020, "全国Ⅰ卷"),)},
    "national_2": {"short": "n2", "kind": "national", "names": ((2015, 2020, "全国Ⅱ卷"),)},
    "national_3": {"short": "n3", "kind": "national", "names": ((2016, 2020, "全国Ⅲ卷"),)},
    "national_jia": {"short": "jia", "kind": "national", "names": ((2021, 2024, "全国甲卷"),)},
    "national_yi": {"short": "yi", "kind": "national", "names": ((2021, 2023, "全国乙卷"),)},
    "new_curriculum_1": {"short": "xgk1", "kind": "national",
                         "names": ((2020, 2022, "新高考Ⅰ卷"), (2023, 2024, "新课标Ⅰ卷"))},
    "new_curriculum_2": {"short": "xgk2", "kind": "national",
                         "names": ((2020, 2022, "新高考Ⅱ卷"), (2023, 2024, "新课标Ⅱ卷"))},
    "national_1_2025": {"short": "qg1", "kind": "national", "names": ((2025, 2099, "全国一卷"),)},
    "national_2_2025": {"short": "qg2", "kind": "national", "names": ((2025, 2099, "全国二卷"),)},
    "legacy_xinkebiao": {"short": "xkb", "kind": "national", "names": ((2007, 2012, "新课标卷"),)},
    "legacy_xinkebiao_1": {"short": "xkb1", "kind": "national", "names": ((2013, 2014, "新课标Ⅰ卷"),)},
    "legacy_xinkebiao_2": {"short": "xkb2", "kind": "national", "names": ((2013, 2014, "新课标Ⅱ卷"),)},
    "legacy_dagang": {"short": "dg", "kind": "national", "names": ((2011, 2014, "大纲卷"),)},
    "legacy_dagang_1": {"short": "dg1", "kind": "national", "names": ((2005, 2010, "大纲全国Ⅰ卷"),)},
    "legacy_dagang_2": {"short": "dg2", "kind": "national", "names": ((2005, 2010, "大纲全国Ⅱ卷"),)},
}
for _key, _name in REGIONS:
    SERIES[_key] = {"short": _key, "kind": "regional", "names": ((1950, 2099, f"{_name}卷"),)}
TRACK_NAMES = {"science": "理科", "liberal": "文科"}
SESSION_NAMES = {"summer": "普通高考", "spring": "春季高考"}


def series_name(series_key: str, year: int) -> str:
    spec = SERIES[series_key]
    for start, end, name in spec["names"]:
        if start <= year <= end:
            return name
    return spec["names"][-1][2]


def paper_id_for(year: int, series_key: str, session: str = "summer", track: str | None = None) -> str:
    if series_key not in SERIES:
        raise ValueError(f"unknown series {series_key}")
    if session not in SESSION_NAMES or (track is not None and track not in TRACK_NAMES):
        raise ValueError("invalid session or track")
    parts = [f"gk{int(year)}", SERIES[series_key]["short"]]
    if track:
        parts.append("l" if track == "science" else "w")
    if session == "spring":
        parts.append("spring")
    return "-".join(parts)


# ---------------------------------------------------------------- rights gate

RIGHTS_STATUSES = ("unresolved", "operator_approved", "licensed", "blocked")


def allowed_rights_scopes() -> frozenset[str]:
    raw = os.environ.get("PAST_PAPER_RIGHTS_SCOPES", "hosted_teacher_classroom")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def rights_allow(rights: dict, scopes: frozenset[str] | None = None) -> bool:
    """A paper is servable only with a license or a scoped operator decision.

    ``unresolved`` (the pipeline default) and ``blocked`` never pass.  An
    operator decision must name who decided, when, on what basis and for which
    deployment scope; a decision recorded for another scope (for example a
    local QA run) does not open a hosted library.
    """
    if not isinstance(rights, dict):
        return False
    status = rights.get("status")
    if status == "licensed":
        return bool(rights.get("license"))
    if status != "operator_approved":
        return False
    decision = rights.get("decision") or {}
    if not all(isinstance(decision.get(k), str) and decision.get(k).strip()
               for k in ("decided_by", "decided_at", "basis", "scope")):
        return False
    return decision["scope"] in (scopes if scopes is not None else allowed_rights_scopes())


# ---------------------------------------------------------------- catalog

class CatalogError(ValueError):
    pass


def library_root() -> Path:
    explicit = os.environ.get("PAST_PAPER_LIBRARY_DIR", "").strip()
    if explicit:
        return Path(explicit)
    import config

    return Path(config.DATA_DIR) / "past_papers"


def _validate_file(entry, paper_id: str, kind: str) -> dict:
    if not isinstance(entry, dict):
        raise CatalogError(f"{paper_id}: {kind} file entry must be an object")
    rel = entry.get("path")
    if not isinstance(rel, str) or not _REL_PATH_RE.fullmatch(rel) or ".." in rel.split("/"):
        raise CatalogError(f"{paper_id}: unsafe {kind} path")
    if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
        raise CatalogError(f"{paper_id}: {kind} sha256 missing")
    previews = entry.get("previews") or []
    if not isinstance(previews, list) or any(
            not isinstance(p, str) or not _REL_PATH_RE.fullmatch(p) or ".." in p.split("/") for p in previews):
        raise CatalogError(f"{paper_id}: unsafe {kind} preview path")
    return entry


def validate_catalog(data) -> dict:
    """Structural validation; returns {paper_id: paper}.  Raises CatalogError."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError("unsupported catalog schema")
    papers = data.get("papers")
    if not isinstance(papers, list):
        raise CatalogError("catalog papers must be a list")
    out = {}
    for paper in papers:
        paper_id = paper.get("paper_id") if isinstance(paper, dict) else None
        if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
            raise CatalogError(f"invalid paper id {paper_id!r}")
        if paper_id in out:
            raise CatalogError(f"duplicate paper id {paper_id}")
        if paper.get("series_key") not in SERIES:
            raise CatalogError(f"{paper_id}: unknown series")
        if paper.get("session") not in SESSION_NAMES or paper.get("track") not in (None, *TRACK_NAMES):
            raise CatalogError(f"{paper_id}: invalid session/track")
        if paper_id != paper_id_for(paper["year"], paper["series_key"], paper["session"], paper["track"]):
            raise CatalogError(f"{paper_id}: id does not match its identity")
        regions = paper.get("regions") or []
        if any(r not in REGION_NAMES for r in regions):
            raise CatalogError(f"{paper_id}: unknown region")
        files = paper.get("files") or {}
        if not isinstance(files, dict) or "student" not in files or set(files) - set(FILE_KINDS):
            raise CatalogError(f"{paper_id}: files must include student and only {FILE_KINDS}")
        for kind, entry in files.items():
            _validate_file(entry, paper_id, kind)
        out[paper_id] = paper
    return out


_cache_lock = threading.Lock()
_cache: dict = {"key": None, "papers": {}, "error": None}


def load_catalog() -> tuple[dict, str | None]:
    """Return ({paper_id: paper}, error).  Cached by catalog mtime/size."""
    path = library_root() / "catalog.json"
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {}, None
    key = (str(path), stat_result.st_mtime_ns, stat_result.st_size)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["papers"], _cache["error"]
        try:
            papers = validate_catalog(json.loads(path.read_text(encoding="utf-8")))
            error = None
        except (OSError, ValueError) as exc:
            papers, error = {}, f"catalog invalid: {exc}"
        _cache.update(key=key, papers=papers, error=error)
        return papers, error


LEGACY_VERIFICATION_METHOD = "multi_lineage_majority_with_independent_review"
RESOURCE_CHECK_METHOD = "purchased_pdf_identity_and_mechanical_check"
_RESOURCE_BOOLEAN_CHECKS = (
    "identity_confirmed", "role_confirmed", "all_pages_rendered",
    "no_known_severe_missing_pages", "visual_sampling_completed",
)
# A model-solved answer key typeset offline from the paper actually served.
# It is its own status: never "verified" (no independent human lineage) and
# never "source_paired" (it did not come with the source PDF).
GENERATED_ANSWER_METHOD = "model_solved_with_programmatic_recheck"
_GENERATED_BOOLEAN_CHECKS = ("all_pages_rendered", "every_question_addressed", "programmatic_recheck_completed")
ANSWER_KINDS = ("final_answers", "solutions", "answers_and_solutions")


def _valid_page_dimension(value) -> bool:
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _file_facts_pass(entry, role: str) -> bool:
    if not isinstance(entry, dict):
        return False
    pages = entry.get("pages")
    dimensions = entry.get("page_dimensions_mm")
    if (entry.get("role") != role or type(pages) is not int or pages <= 0
            or type(entry.get("bytes")) is not int or entry["bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
            or not isinstance(dimensions, list) or len(dimensions) != pages):
        return False
    for size in dimensions:
        if not isinstance(size, dict) or any(not _valid_page_dimension(size.get(axis))
                                             for axis in ("width", "height")):
            return False
    return not (entry.get("page_size") == "A4" and any(
        abs(min(size["width"], size["height"]) - 210) > 0.6
        or abs(max(size["width"], size["height"]) - 297) > 0.6 for size in dimensions))


def _checks_bound_to_file(entry: dict, checks: dict, required: tuple) -> bool:
    return (all(checks.get(key) is True for key in required)
            and type(checks.get("blocking_issue_count")) is int and checks["blocking_issue_count"] == 0
            and checks.get("file_sha256") == entry["sha256"]
            and type(checks.get("rendered_pages")) is int and checks["rendered_pages"] == entry["pages"])


def _resource_check_passes(entry, record, role: str) -> bool:
    """Require explicit facts bound to these PDF bytes, never truthy defaults.

    Renderability and stratified sampling are NOT a per-question maths review.
    Check receipts/provenance remain offline; this gate validates their declared
    contract, not the underlying visual judgement or permission to redistribute.
    """
    if not isinstance(record, dict) or not _file_facts_pass(entry, role):
        return False
    checks = record.get("checks")
    if record.get("method") != RESOURCE_CHECK_METHOD or not isinstance(checks, dict):
        return False
    required = (*_RESOURCE_BOOLEAN_CHECKS, "student_answer_free" if role == "student" else "pairing_confirmed")
    return _checks_bound_to_file(entry, checks, required)


def _generated_answer_passes(entry, record) -> bool:
    """A generated key is only a reference: the gate checks its declared offline
    receipt (rendered, every question addressed, results re-checked by program),
    not mathematical correctness, which nobody has independently reviewed."""
    if not isinstance(record, dict) or not _file_facts_pass(entry, "answer"):
        return False
    checks = record.get("checks")
    if record.get("method") != GENERATED_ANSWER_METHOD or not isinstance(checks, dict):
        return False
    return _checks_bound_to_file(entry, checks, _GENERATED_BOOLEAN_CHECKS)


def _student_checks_pass(paper: dict) -> bool:
    verification = paper.get("verification")
    if not isinstance(verification, dict):
        return False
    if verification.get("status") == "verified":
        # Keep the old whole-content/independent-review meaning; do not let the
        # purchased-PDF method borrow its label. All existing releases name it.
        return verification.get("method") == LEGACY_VERIFICATION_METHOD
    edition = paper.get("edition")
    return (verification.get("status") == "resource_checked"
            and isinstance(edition, dict) and edition.get("kind") == "purchased_pdf"
            and _resource_check_passes((paper.get("files") or {}).get("student"), verification, "student"))


def is_available(paper: dict, scopes: frozenset[str] | None = None) -> bool:
    return _student_checks_pass(paper) and rights_allow(paper.get("rights") or {}, scopes)


def answer_available(paper: dict) -> bool:
    files = paper.get("files") or {}
    answer = paper.get("answer")
    if not files.get("answer") or not isinstance(answer, dict) or not _student_checks_pass(paper):
        return False
    paired = (answer.get("kind") in ANSWER_KINDS
              and answer.get("paired_paper_id") == paper.get("paper_id")
              and answer.get("paired_student_sha256") == files["student"]["sha256"])
    if answer.get("status") == "generated":
        # Allowed for both editions; bound to the exact student file it solves.
        return paired and _generated_answer_passes(files["answer"], answer)
    if (paper.get("verification") or {}).get("status") == "verified":
        return answer.get("status") == "verified"
    return (answer.get("status") == "source_paired" and paired
            and _resource_check_passes(files["answer"], answer, "answer"))


def resolve_file(paper: dict, kind: str, *, preview_page: int | None = None) -> tuple[Path, str | None]:
    """Resolve a catalog file inside the library root.

    Returns (path, expected_sha256); the sha is None for previews.  Raises
    FileNotFoundError for anything missing, unverified or outside the root.
    """
    if kind not in FILE_KINDS:
        raise FileNotFoundError(kind)
    if kind == "answer" and not answer_available(paper):
        raise FileNotFoundError("answer unavailable")
    entry = (paper.get("files") or {}).get(kind)
    if not entry:
        raise FileNotFoundError(kind)
    root = library_root().resolve()
    if preview_page is None:
        rel, expected = entry["path"], entry["sha256"]
    else:
        previews = entry.get("previews") or []
        if not 1 <= preview_page <= len(previews):
            raise FileNotFoundError("preview page")
        rel, expected = previews[preview_page - 1], None
    candidate = (root / rel).resolve()
    if os.path.commonpath([str(candidate), str(root)]) != str(root) or not candidate.is_file():
        raise FileNotFoundError(rel)
    return candidate, expected


def sha256_matches(path: Path, expected: str) -> bool:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


# ---------------------------------------------------------------- payloads

def _checks_payload(record: dict) -> dict:
    checks = record.get("checks")
    if not isinstance(checks, dict):
        return {}
    return {key: checks[key] for key in (*_RESOURCE_BOOLEAN_CHECKS, *_GENERATED_BOOLEAN_CHECKS, "blocking_issue_count",
                                        "file_sha256", "rendered_pages", "student_answer_free", "pairing_confirmed")
            if key in checks}


def _file_metadata(entry: dict) -> dict:
    return {**{key: entry.get(key) for key in ("pages", "page_size", "page_dimensions_mm", "print_note")},
            "preview_pages": len(entry.get("previews") or [])}


def _verification_payload(paper: dict) -> dict:
    record = paper.get("verification") or {}
    return {**{key: record.get(key) for key in ("status", "method", "summary", "lineages", "reviewed_at")},
            "checks": _checks_payload(record)}


def _answer_payload(paper: dict) -> dict:
    # Do not advertise candidate/mismatched/blocked answer metadata as content.
    available = answer_available(paper)
    answer = (paper.get("answer") or {}) if available else {}
    entry = ((paper.get("files") or {}).get("answer") or {}) if available else {}
    return {"available": available, "status": answer.get("status", "unavailable"),
            "method": answer.get("method"), "kind": answer.get("kind", ""), "note": answer.get("note", ""),
            "checks": _checks_payload(answer), **_file_metadata(entry)}


def summary_payload(paper: dict) -> dict:
    files = paper.get("files") or {}
    student = files.get("student") or {}
    return {
        "paper_id": paper["paper_id"],
        "year": paper["year"],
        "session": paper["session"],
        "session_name": SESSION_NAMES[paper["session"]],
        "series_key": paper["series_key"],
        "series_name": series_name(paper["series_key"], paper["year"]),
        "kind": SERIES[paper["series_key"]]["kind"],
        "track": paper.get("track"),
        "track_name": TRACK_NAMES.get(paper.get("track"), ""),
        "title": paper.get("title", ""),
        "regions": [{"key": r, "name": REGION_NAMES[r]} for r in paper.get("regions") or []],
        "question_count": paper.get("question_count"),
        "total_points": paper.get("total_points"),
        "pages": student.get("pages"),
        "preview_pages": len(student.get("previews") or []),
        "edition": paper.get("edition") or {},
        "student": _file_metadata(student),
        "verification": _verification_payload(paper),
        "answer": _answer_payload(paper),
        "answer_available": answer_available(paper),
    }


def detail_payload(paper: dict) -> dict:
    payload = summary_payload(paper)
    payload.update({
        "rights": {
            "status": (paper.get("rights") or {}).get("status"),
            "notice": (paper.get("rights") or {}).get("notice", ""),
        },
        "attribution": paper.get("attribution") or [],
        "notes": paper.get("notes") or [],
    })
    return payload


def download_filename(paper: dict, kind: str) -> str:
    name = series_name(paper["series_key"], paper["year"])
    track = TRACK_NAMES.get(paper.get("track"), "")
    session = "春季" if paper["session"] == "spring" else ""
    label = "试卷" if kind == "student" else "参考答案"
    stem = f"{paper['year']}{session}高考数学{name}{('（' + track + '）') if track else ''}_{label}"
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem) + ".pdf"
