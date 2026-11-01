"""整卷真题 library: builder gates, catalog safety, rights gate, auth and downloads.

The fixtures build a synthetic workspace (no real exam content) and run the
real library builder, then exercise the real router through a minimal app.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import past_papers as router_module
from routers.auth import _get_current_user
from scripts.past_papers import build_library, inventory
from services import past_papers as library

PDF_BYTES = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _render(paper_dir: Path, kind: str, source_text: str, *, pages: int = 1, blocking=None,
            stale: bool = False) -> str:
    source = paper_dir / f"{kind}.paper.txt"
    source.write_bytes(source_text.encode("utf-8"))  # exact bytes: the builder hashes the file
    build = paper_dir / "build" / kind
    (build / "preview").mkdir(parents=True, exist_ok=True)
    pdf = PDF_BYTES + kind.encode() + paper_dir.name.encode()
    (build / f"{kind}.pdf").write_bytes(pdf)
    for page in range(1, pages + 1):
        (build / "preview" / f"page-{page}.png").write_bytes(PNG_BYTES + bytes([page]))
    source_sha = _sha(source_text.encode())
    _write_json(build / "render_report.json", {
        "pdf": f"{kind}.pdf", "pdf_sha256": _sha(pdf), "pages": pages, "blocking": blocking or [],
        "source_sha256": "0" * 64 if stale else source_sha,
    })
    return source_sha


def _paper(ws: Path, paper_id: str, *, status="verified", review=True, answer=False, stale=False,
           blocking=None, title="2024年普通高等学校招生全国统一考试（测试卷）") -> Path:
    paper_dir = ws / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    student_sha = _render(paper_dir, "student", f"%paper_id {paper_id}\n", pages=2, blocking=blocking, stale=stale)
    verification = {"paper_id": paper_id, "status": status, "quarantine_reasons": [] if status == "verified" else ["test"],
                    "exam": {"title": title, "question_count": 19, "total_points": 150},
                    "questions": [{"q": "1", "deviations": [{"lineage": "deekur", "detail": "x"}]}],
                    "answers": {"status": "verified" if answer else "unavailable"},
                    "verifier": "test verifier"}
    _write_json(paper_dir / "verification.json", verification)
    _write_json(paper_dir / "sources.json", {"lineages": [{"id": "deekur"}, {"id": "eol"}, {"id": "other"}]})
    _write_json(paper_dir / "figures.json", {"figures": {"q1": {"source": {"provider": "deekur/gaokaomath"}}}})
    figures_sha = _sha((paper_dir / "figures.json").read_bytes())
    report_path = paper_dir / "build" / "student" / "render_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    _write_json(report_path, {**report, "figures_sha256": figures_sha})
    review_doc = {"status": "pass", "reviewer": "test reviewer", "student_source_sha256": student_sha,
                  "student_figures_sha256": figures_sha, "reviewed_at": "2026-10-08T00:00:00Z"}
    if answer:
        answer_sha = _render(paper_dir, "answer", f"%paper_id {paper_id} answers\n")
        review_doc.update(answer_status="pass", answer_source_sha256=answer_sha)
    if review:
        _write_json(paper_dir / "review.json", review_doc)
    return paper_dir


CANDIDATES = {
    "gk2024-xgk1": (2024, "new_curriculum_1", None),
    "gk2023-xgk1": (2023, "new_curriculum_1", None),
    "gk2024-beijing": (2024, "beijing", None),
    "gk2019-n1-l": (2019, "national_1", "science"),
    "gk2019-n1-w": (2019, "national_1", "liberal"),
    "gk2024-jia-l": (2024, "national_jia", "science"),
    "gk2024-tianjin": (2024, "tianjin", None),
    "gk2024-shanghai": (2024, "shanghai", None),
    "gk2024-xgk2": (2024, "new_curriculum_2", None),
}
REGIONS = {"years": {
    "2024": {"new_curriculum_1": {"regions": ["shandong", "guangdong", "henan"], "confidence": "confirmed"}},
    "2023": {"new_curriculum_1": {"regions": ["shandong", "guangdong"], "confidence": "confirmed"}},
    "2019": {"national_1": {"regions": ["henan", "shandong"], "confidence": "confirmed"}},
}}


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    candidates = {pid: {"paper_id": pid, "year": y, "session": "summer", "series_key": s, "track": t,
                        "files": [{"sha256": "f" * 64}]} for pid, (y, s, t) in CANDIDATES.items()}
    _write_json(ws / "inventory" / "candidates.json", candidates)
    _paper(ws, "gk2024-xgk1", answer=True)
    _paper(ws, "gk2023-xgk1")
    _paper(ws, "gk2024-beijing")
    _paper(ws, "gk2019-n1-l")
    _paper(ws, "gk2019-n1-w")
    jia_l = _paper(ws, "gk2024-jia-l", status="quarantined")
    # Structured quarantine reasons must not break the coverage manifest.
    doc = json.loads((jia_l / "verification.json").read_text(encoding="utf-8"))
    doc["quarantine_reasons"].append({"q": "2", "reason": "figure label missing"})
    _write_json(jia_l / "verification.json", doc)
    _paper(ws, "gk2024-tianjin", review=False)
    _paper(ws, "gk2024-shanghai", stale=True)
    _paper(ws, "gk2024-xgk2", blocking=[{"kind": "katex_error"}])
    regions = tmp_path / "regions.json"
    _write_json(regions, REGIONS)
    return ws, regions


APPROVE_ALL = {"decisions": [{"applies_to": {"all_verified": True}, "decided_by": "test operator",
                              "decided_at": "2026-10-08", "basis": "test basis",
                              "scope": "hosted_teacher_classroom"}]}


@pytest.fixture
def built(workspace, tmp_path, monkeypatch):
    ws, regions = workspace
    rights = tmp_path / "rights.json"
    _write_json(rights, APPROVE_ALL)
    out = tmp_path / "library"
    summary = build_library.build(ws, out, regions_path=regions, rights_path=rights)
    monkeypatch.setenv("PAST_PAPER_LIBRARY_DIR", str(out))
    monkeypatch.delenv("PAST_PAPER_RIGHTS_SCOPES", raising=False)
    return out, summary, ws


class _User:
    def __init__(self, role="teacher", stage="senior"):
        self.id = 7
        self.role = role
        self.education_stage = stage


def _client(user):
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api")
    app.dependency_overrides[_get_current_user] = lambda: user
    return TestClient(app)


# ------------------------------------------------------------------ identity

def test_inventory_names_never_merge_similar_series():
    parse = inventory.parse_deekur_name
    cases = {
        "2013新课标1理(河南,河北,山西).pdf": "gk2013-xkb1-l",
        "2023新高考1(山东,广东).pdf": "gk2023-xgk1",
        "2024全国理(西藏,四川).pdf": "gk2024-jia-l",
        "2022全国甲文(云南).pdf": "gk2022-jia-w",
        "2025全国1(山东).pdf": "gk2025-qg1",
        "2019全国3理(云南).pdf": "gk2019-n3-l",
        "2019北京文.pdf": "gk2019-beijing-w",
        "2023上海.pdf": "gk2023-shanghai",
        "2024春季上海.pdf": "gk2024-shanghai-spring",
        "2026上海春季.pdf": "gk2026-shanghai-spring",
    }
    for name, expected in cases.items():
        year, series_key, session, track, _ = parse(name)
        assert library.paper_id_for(year, series_key, session, track) == expected, name
    # The 2013 and 2023 "新课标Ⅰ卷" share a printed name but are different series.
    assert library.series_name("legacy_xinkebiao_1", 2013) == library.series_name("new_curriculum_1", 2023)
    assert "legacy_xinkebiao_1" != "new_curriculum_1"
    assert parse("2024全国1(山东).pdf") is None  # 全国1 only exists from 2025


def test_inventory_dedups_bytes_and_groups_editions(tmp_path):
    raw_a, raw_b = tmp_path / "a" / "普通高考" / "2024", tmp_path / "b" / "普通高考" / "2024"
    raw_a.mkdir(parents=True)
    raw_b.mkdir(parents=True)
    name = "2024新高考1(山东,广东).pdf"
    (raw_a / name).write_bytes(PDF_BYTES)
    (raw_b / name).write_bytes(PDF_BYTES)  # identical copy
    (raw_b / "2024北京.pdf").write_bytes(PDF_BYTES + b"bj")
    ws = tmp_path / "ws"
    first = inventory.run(ws, [tmp_path / "a", tmp_path / "b"])
    assert first == {"files_seen": 3, "unique_files": 2, "candidate_papers": 2}
    candidates = json.loads((ws / "inventory" / "candidates.json").read_text(encoding="utf-8"))
    assert len(candidates["gk2024-xgk1"]["files"]) == 1
    assert candidates["gk2024-xgk1"]["claimed_regions"]["deekur/gaokaomath"] == ["山东", "广东"]
    # A second edition of the same exam joins the same canonical paper.
    (raw_b / name).write_bytes(PDF_BYTES + b"second edition")
    inventory.run(ws, [tmp_path / "a", tmp_path / "b"])
    candidates = json.loads((ws / "inventory" / "candidates.json").read_text(encoding="utf-8"))
    assert len(candidates) == 2 and len(candidates["gk2024-xgk1"]["files"]) == 2
    assert len(list((ws / "originals").iterdir())) == 3  # content-addressed, never duplicated


def test_reference_region_mapping_assigns_every_region_once_per_year():
    path = Path(__file__).resolve().parents[1] / "references" / "past_papers" / "region_usage.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for year, papers in doc["years"].items():
        seen = {}
        for series_key, mapping in papers.items():
            assert series_key in library.SERIES, (year, series_key)
            assert mapping["sources"] and all(s in doc["sources"] for s in mapping["sources"]), (year, series_key)
            for region in mapping["regions"]:
                assert region in library.REGION_NAMES, (year, region)
                assert region not in seen, f"{year}: {region} in {seen.get(region)} and {series_key}"
                seen[region] = series_key
        assert set(seen) == set(library.REGION_NAMES), (year, set(library.REGION_NAMES) - set(seen))
    # Mappings are year-specific: 山东 moved from 全国Ⅰ卷 (2019) to 新高考Ⅰ卷 (2020).
    assert "shandong" in doc["years"]["2019"]["national_1"]["regions"]
    assert "shandong" not in doc["years"]["2020"]["national_1"]["regions"]
    assert "shandong" in doc["years"]["2020"]["new_curriculum_1"]["regions"]


def test_region_mapping_is_year_specific(workspace):
    _, regions_path = workspace
    doc = json.loads(regions_path.read_text(encoding="utf-8"))
    assert build_library.region_lookup(doc, 2024, "new_curriculum_1")[0] == ["shandong", "guangdong", "henan"]
    assert "henan" not in build_library.region_lookup(doc, 2023, "new_curriculum_1")[0]
    assert build_library.region_lookup(doc, 2024, "beijing") == (["beijing"], "self_set")
    assert build_library.region_lookup(doc, 2022, "new_curriculum_1") == ([], "unmapped")


# ------------------------------------------------------------------ builder

def test_builder_publishes_only_verified_reviewed_fresh_papers(built):
    out, summary, ws = built
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    ids = {p["paper_id"] for p in catalog["papers"]}
    assert ids == {"gk2024-xgk1", "gk2023-xgk1", "gk2024-beijing", "gk2019-n1-l", "gk2019-n1-w"}
    assert summary["published_answers"] == 1
    coverage = {row["paper_id"]: row for row in json.loads((ws / "coverage" / "coverage.json").read_text(encoding="utf-8"))}
    assert coverage["gk2024-jia-l"]["status"] == "quarantined"
    assert "figure label missing" in coverage["gk2024-jia-l"]["reasons"]
    assert "independent review" in coverage["gk2024-tianjin"]["reasons"]
    assert "stale PDF" in coverage["gk2024-shanghai"]["reasons"]
    assert "render blocking" in coverage["gk2024-xgk2"]["reasons"]
    paper = next(p for p in catalog["papers"] if p["paper_id"] == "gk2024-xgk1")
    assert paper["edition"]["kind"] == "retypeset"
    assert paper["attribution"][0]["license"] == "CC BY 4.0"
    assert paper["files"]["student"]["path"].startswith("files/gk2024-xgk1/student-")


def test_review_must_be_independent_and_bound_to_the_rendered_figures():
    verification = {"status": "verified", "verifier": "agent A"}
    report = {"source_sha256": "s" * 64, "figures_sha256": "f" * 64}
    good = {"status": "pass", "reviewer": "agent B", "student_source_sha256": "s" * 64,
            "student_figures_sha256": "f" * 64}
    assert build_library.review_problem(good, verification, report) is None
    assert "other than the verifier" in build_library.review_problem(
        {**good, "reviewer": "agent A"}, verification, report)
    assert "other than the verifier" in build_library.review_problem(
        {**good, "reviewer": ""}, verification, report)
    # A figure re-cut after review changes figures.json, so the old review no longer applies.
    assert "different figure set" in build_library.review_problem(
        good, verification, {**report, "figures_sha256": "g" * 64})
    assert "different transcription" in build_library.review_problem(
        good, verification, {**report, "source_sha256": "t" * 64})
    assert "missing or not passed" in build_library.review_problem(
        {**good, "status": "fail"}, verification, report)


def test_builder_is_idempotent_and_prunes_withdrawn_papers(built, workspace, tmp_path):
    out, _, ws = built
    _, regions = workspace
    first = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    files_before = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    build_library.build(ws, out, regions_path=regions, rights_path=tmp_path / "rights.json")
    second = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    first.pop("generated_at"), second.pop("generated_at")
    assert first == second
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == files_before
    # Withdraw one paper: its files leave the hosted library on the next build.
    verification = ws / "papers" / "gk2023-xgk1" / "verification.json"
    doc = json.loads(verification.read_text(encoding="utf-8"))
    doc["status"] = "quarantined"
    verification.write_text(json.dumps(doc), encoding="utf-8")
    build_library.build(ws, out, regions_path=regions, rights_path=tmp_path / "rights.json")
    assert not list(out.glob("files/gk2023-xgk1/*")) and not list(out.glob("previews/gk2023-xgk1/*"))


def test_documented_exclusions_never_publish(workspace, tmp_path):
    ws, regions = workspace
    _write_json(ws / "inventory" / "exclusions.json", {"gk2024-beijing": "only recollected copies circulate"})
    out = tmp_path / "excl"
    build_library.build(ws, out, regions_path=regions)
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert "gk2024-beijing" not in {p["paper_id"] for p in catalog["papers"]}
    rows = {r["paper_id"]: r for r in json.loads((ws / "coverage" / "coverage.json").read_text(encoding="utf-8"))}
    assert rows["gk2024-beijing"]["status"] == "excluded"
    assert rows["gk2024-beijing"]["reasons"] == "only recollected copies circulate"


# ------------------------------------------------------------------ rights gate

def test_rights_gate_requires_scoped_operator_decision():
    decision = {"decided_by": "op", "decided_at": "2026-10-08", "basis": "b", "scope": "hosted_teacher_classroom"}
    scopes = frozenset({"hosted_teacher_classroom"})
    assert library.rights_allow({"status": "operator_approved", "decision": decision}, scopes)
    assert not library.rights_allow({"status": "unresolved"}, scopes)
    assert not library.rights_allow({"status": "blocked", "decision": decision}, scopes)
    assert not library.rights_allow({"status": "operator_approved", "decision": {**decision, "basis": ""}}, scopes)
    assert not library.rights_allow({"status": "operator_approved", "decision": {**decision, "scope": "local_qa"}}, scopes)
    assert not library.rights_allow({"status": "licensed"}, scopes)
    assert library.rights_allow({"status": "licensed", "license": "written permission #1"}, scopes)


def test_unresolved_rights_keep_verified_papers_out_of_teacher_downloads(workspace, tmp_path, monkeypatch):
    ws, regions = workspace
    out = tmp_path / "unresolved"
    build_library.build(ws, out, regions_path=regions)  # no operator decision
    monkeypatch.setenv("PAST_PAPER_LIBRARY_DIR", str(out))
    teacher = _client(_User()).get("/api/past-papers").json()
    assert teacher["items"] == [] and "withheld" not in teacher
    assert _client(_User()).get("/api/past-papers/gk2024-xgk1/download/student").status_code == 404
    admin = _client(_User(role="admin")).get("/api/past-papers").json()
    assert admin["withheld"] == {"rights_pending": 5, "not_verified": 0}


def test_decision_scoped_to_another_deployment_does_not_open_library(workspace, tmp_path, monkeypatch):
    ws, regions = workspace
    rights = tmp_path / "qa.json"
    qa = json.loads(json.dumps(APPROVE_ALL))
    qa["decisions"][0]["scope"] = "local_qa"
    _write_json(rights, qa)
    out = tmp_path / "qa-library"
    build_library.build(ws, out, regions_path=regions, rights_path=rights)
    monkeypatch.setenv("PAST_PAPER_LIBRARY_DIR", str(out))
    monkeypatch.delenv("PAST_PAPER_RIGHTS_SCOPES", raising=False)
    assert _client(_User()).get("/api/past-papers").json()["total"] == 0
    monkeypatch.setenv("PAST_PAPER_RIGHTS_SCOPES", "local_qa")
    assert _client(_User()).get("/api/past-papers").json()["total"] == 5


# ------------------------------------------------------------------ API

def test_every_route_requires_login(built):
    client = _client(None)
    for url in ("/api/past-papers", "/api/past-papers/gk2024-xgk1",
                "/api/past-papers/gk2024-xgk1/download/student", "/api/past-papers/gk2024-xgk1/preview/student/1"):
        assert client.get(url).status_code == 401, url


def test_other_stages_get_an_explanation_not_senior_papers(built):
    client = _client(_User(stage="junior"))
    data = client.get("/api/past-papers").json()
    assert data["stage_supported"] is False and data["items"] == [] and "高中" in data["message"]
    assert client.get("/api/past-papers/gk2024-xgk1").status_code == 404
    assert client.get("/api/past-papers/gk2024-xgk1/download/student").status_code == 404


def test_filters_by_region_year_series_and_track(built):
    client = _client(_User())
    by_region = client.get("/api/past-papers", params={"region": "henan"}).json()
    assert [i["paper_id"] for i in by_region["items"]] == ["gk2024-xgk1", "gk2019-n1-l", "gk2019-n1-w"]
    beijing = client.get("/api/past-papers", params={"region": "beijing"}).json()
    assert [i["paper_id"] for i in beijing["items"]] == ["gk2024-beijing"]
    series = client.get("/api/past-papers", params={"series_key": "new_curriculum_1"}).json()
    assert [(i["year"], i["series_name"]) for i in series["items"]] == [(2024, "新课标Ⅰ卷"), (2023, "新课标Ⅰ卷")]
    science = client.get("/api/past-papers", params={"track": "science"}).json()
    assert [i["paper_id"] for i in science["items"]] == ["gk2019-n1-l"]
    untracked = client.get("/api/past-papers", params={"track": "none", "year": 2024}).json()
    assert {i["paper_id"] for i in untracked["items"]} == {"gk2024-xgk1", "gk2024-beijing"}
    facets = client.get("/api/past-papers").json()["facets"]
    assert facets["years"] == [2024, 2023, 2019]
    assert {r["key"] for r in facets["regions"]} == {"beijing", "shandong", "guangdong", "henan"}
    assert client.get("/api/past-papers", params={"region": "atlantis"}).status_code == 422


def test_student_and_answer_downloads_are_separate(built):
    out, _, _ = built
    client = _client(_User())
    student = client.get("/api/past-papers/gk2024-xgk1/download/student")
    assert student.status_code == 200 and student.content.startswith(b"%PDF")
    assert "filename*=UTF-8''" in student.headers["content-disposition"]
    answer = client.get("/api/past-papers/gk2024-xgk1/download/answer")
    assert answer.status_code == 200 and answer.content != student.content
    detail = client.get("/api/past-papers/gk2024-xgk1").json()
    assert detail["answer"]["available"] is True and detail["pages"] == 2
    no_answer = client.get("/api/past-papers/gk2024-beijing").json()
    assert no_answer["answer"]["available"] is False and no_answer["answer_available"] is False
    missing = client.get("/api/past-papers/gk2024-beijing/download/answer")
    assert missing.status_code == 404 and "答案" in missing.json()["detail"]
    preview = client.get("/api/past-papers/gk2024-xgk1/preview/student/2")
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    assert client.get("/api/past-papers/gk2024-xgk1/preview/student/3").status_code == 404


def test_identity_only_file_access_and_integrity(built):
    out, _, _ = built
    client = _client(_User())
    for url in ("/api/past-papers/..%2F..%2Fcatalog/download/student",
                "/api/past-papers/gk2024-xgk1/download/..%2Fcatalog.json",
                "/api/past-papers/gk2024-xgk1/download/catalog",
                "/api/past-papers/gk2024-jia-l/download/student",  # quarantined: never published
                "/api/past-papers/gk2099-xgk1"):
        assert client.get(url).status_code in (404, 422), url
    body = client.get("/api/past-papers/gk2024-xgk1").json()
    assert str(out) not in json.dumps(body, ensure_ascii=False)
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    rel = next(p for p in catalog["papers"] if p["paper_id"] == "gk2024-beijing")["files"]["student"]["path"]
    (out / rel).write_bytes(PDF_BYTES + b"tampered")
    response = client.get("/api/past-papers/gk2024-beijing/download/student")
    assert response.status_code == 409


def test_catalog_validation_rejects_unsafe_entries():
    base = {"paper_id": "gk2024-xgk1", "year": 2024, "session": "summer", "series_key": "new_curriculum_1",
            "track": None, "regions": ["shandong"],
            "files": {"student": {"path": "files/gk2024-xgk1/student.pdf", "sha256": "a" * 64}}}

    def check(**changes):
        paper = json.loads(json.dumps(base))
        paper.update(changes)
        library.validate_catalog({"schema_version": 1, "papers": [paper]})

    check()
    for bad in ("../secret.pdf", "/etc/passwd", "C:/x.pdf", "files/../../x.pdf", "files\\x.pdf"):
        with pytest.raises(library.CatalogError):
            check(files={"student": {"path": bad, "sha256": "a" * 64}})
    with pytest.raises(library.CatalogError):
        check(paper_id="gk2024-xgk2")  # id must match identity
    with pytest.raises(library.CatalogError):
        check(regions=["atlantis"])
    with pytest.raises(library.CatalogError):
        check(files={"student": {"path": "files/a.pdf", "sha256": "a" * 64,
                                 "previews": ["../a.png"]}})
    with pytest.raises(library.CatalogError):
        check(files={"answer": {"path": "files/a.pdf", "sha256": "a" * 64}})  # student required


# Purchased PDFs use a separate, deliberately narrower resource contract.
# These are synthetic bytes, not a claim that a real paper was inspected.
def _purchased_paper():
    student = {
        "path": "files/gk2025-qg1/student.pdf", "sha256": _sha(PDF_BYTES),
        "bytes": len(PDF_BYTES), "pages": 2, "role": "student", "page_size": "Letter",
        "page_dimensions_mm": [{"width": 215.9, "height": 279.4}] * 2,
        "print_note": "保留原文件水印；打印时按纸张选择缩放。",
        "previews": ["previews/gk2025-qg1/student-1.png", "previews/gk2025-qg1/student-2.png"],
    }
    answer_bytes = PDF_BYTES + b"source answer"
    answer_file = {
        **student, "path": "files/gk2025-qg1/answer.pdf", "sha256": _sha(answer_bytes),
        "bytes": len(answer_bytes), "role": "answer", "pages": 1,
        "page_dimensions_mm": [{"width": 215.9, "height": 279.4}],
        "previews": ["previews/gk2025-qg1/answer-1.png"],
    }

    def checks(entry):
        return {"identity_confirmed": True, "role_confirmed": True, "all_pages_rendered": True,
                "no_known_severe_missing_pages": True, "visual_sampling_completed": True,
                "blocking_issue_count": 0, "file_sha256": entry["sha256"], "rendered_pages": entry["pages"]}

    return {
        "paper_id": "gk2025-qg1", "year": 2025, "session": "summer", "series_key": "national_1_2025",
        "track": None, "title": "2025年高考数学全国一卷", "regions": ["shandong"],
        "edition": {"kind": "purchased_pdf", "label": "购买资料 PDF", "description": "保留购买资料原文件，非考试院原册认证。"},
        "files": {"student": student, "answer": answer_file},
        "verification": {"status": "resource_checked", "method": "purchased_pdf_identity_and_mechanical_check",
                         "summary": "身份、角色、全页机械渲染与分层视觉抽样；未独立逐题核验数学内容。",
                         "checks": {**checks(student), "student_answer_free": True}},
        "answer": {"status": "source_paired", "method": "purchased_pdf_identity_and_mechanical_check",
                   "kind": "solutions", "note": "来源配套解析，未独立逐题核验，不是官方答案。",
                   "paired_paper_id": "gk2025-qg1", "paired_student_sha256": student["sha256"],
                   "checks": {**checks(answer_file), "pairing_confirmed": True}},
        "rights": {"status": "operator_approved", "decision": {
            "decided_by": "test operator", "decided_at": "2026-10-12", "basis": "test scoped risk decision, not licence",
            "scope": "hosted_teacher_classroom"}, "notice": "运营风险决定，不是出版社再分发许可。"},
        "notes": ["保留原文件；水印与暂缓三角标如实登记。"], "attribution": [],
    }


def _install_purchased(tmp_path, monkeypatch, paper):
    root = tmp_path / "purchased-library"
    for kind, entry in paper["files"].items():
        path = root / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PDF_BYTES if kind == "student" else PDF_BYTES + b"source answer")
        for rel in entry["previews"]:
            preview = root / rel
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(PNG_BYTES)
    _write_json(root / "catalog.json", {"schema_version": 1, "papers": [paper]})
    monkeypatch.setenv("PAST_PAPER_LIBRARY_DIR", str(root))
    monkeypatch.delenv("PAST_PAPER_RIGHTS_SCOPES", raising=False)
    monkeypatch.setattr(library, "_cache", {"key": None, "papers": {}, "error": None})
    return root


def test_purchased_resource_check_opens_real_http_consumers(tmp_path, monkeypatch):
    paper = _purchased_paper()
    root = _install_purchased(tmp_path, monkeypatch, paper)
    assert library.is_available(paper)
    assert library.answer_available(paper)
    client = _client(_User())
    listing = client.get("/api/past-papers").json()
    assert listing["total"] == 1 and listing["facets"]["years"] == [2025]
    item = listing["items"][0]
    detail = client.get("/api/past-papers/gk2025-qg1").json()
    for payload in (item, detail):
        assert payload["verification"]["status"] == "resource_checked"
        assert payload["verification"]["checks"]["all_pages_rendered"] is True
        assert payload["student"]["page_size"] == "Letter"
        assert payload["student"]["page_dimensions_mm"] == paper["files"]["student"]["page_dimensions_mm"]
        assert payload["student"]["print_note"] == paper["files"]["student"]["print_note"]
        assert payload["edition"] == paper["edition"]
        assert payload["answer"]["status"] == "source_paired"
        assert payload["answer"]["kind"] == "solutions"
        assert payload["answer"]["checks"]["pairing_confirmed"] is True
        assert payload["answer"]["preview_pages"] == 1
        assert "path" not in payload["student"] and str(root) not in json.dumps(payload)
    for kind in ("student", "answer"):
        response = client.get(f"/api/past-papers/gk2025-qg1/download/{kind}")
        assert response.status_code == 200 and _sha(response.content) == paper["files"][kind]["sha256"]
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert client.get(f"/api/past-papers/gk2025-qg1/preview/{kind}/1").content == PNG_BYTES
    assert client.get("/api/past-papers/gk2025-qg1/preview/answer/2").status_code == 404
    for user in (None, _User(stage="junior")):
        denied = _client(user)
        for suffix in ("", "/download/student", "/download/answer", "/preview/student/1", "/preview/answer/1"):
            assert denied.get("/api/past-papers/gk2025-qg1" + suffix).status_code == (401 if user is None else 404)
    (root / paper["files"]["student"]["path"]).write_bytes(b"tampered")
    assert client.get("/api/past-papers/gk2025-qg1/download/student").status_code == 409


_MISSING = object()
_RESOURCE_FACTS = ("identity_confirmed", "role_confirmed", "all_pages_rendered",
                   "no_known_severe_missing_pages", "visual_sampling_completed")


def _invalid_check_cases(record, kind, extra_fact):
    cases = [(f"{record}.method", "unknown"), (f"{record}.method", _MISSING),
             (f"{record}.checks", {}), (f"{record}.checks", "checked"),
             (f"{record}.checks.blocking_issue_count", _MISSING),
             (f"{record}.checks.blocking_issue_count", 1), (f"{record}.checks.blocking_issue_count", False),
             (f"{record}.checks.blocking_issue_count", "0"), (f"{record}.checks.blocking_issue_count", -1),
             (f"{record}.checks.file_sha256", "0" * 64), (f"{record}.checks.file_sha256", _MISSING),
             (f"{record}.checks.rendered_pages", 0), (f"{record}.checks.rendered_pages", 99),
             (f"{record}.checks.rendered_pages", True), (f"{record}.checks.rendered_pages", _MISSING),
             (f"files.{kind}.role", "unknown"), (f"files.{kind}.role", _MISSING),
             (f"files.{kind}.pages", 0), (f"files.{kind}.pages", True),
             (f"files.{kind}.bytes", 0), (f"files.{kind}.bytes", _MISSING),
             (f"files.{kind}.page_dimensions_mm", []), (f"files.{kind}.page_dimensions_mm", _MISSING),
             (f"files.{kind}.page_dimensions_mm", [{"width": 0, "height": 297}] * (2 if kind == "student" else 1)),
             (f"files.{kind}.page_dimensions_mm", [{"width": True, "height": 297}] * (2 if kind == "student" else 1)),
             (f"files.{kind}.page_size", "A4")]
    cases += [(f"files.{kind}.page_dimensions_mm", [{"width": bad, "height": 297}] * (2 if kind == "student" else 1))
              for bad in (float("nan"), float("inf"), 10 ** 400)]
    cases += [(f"{record}.checks.{key}", bad) for key in (*_RESOURCE_FACTS, extra_fact)
              for bad in (_MISSING, False, 1, "true", None)]
    return cases


def _change_field(paper, field, value):
    parts = field.split(".")
    target = paper
    for part in parts[:-1]:
        target = target[part]
    if value is _MISSING:
        target.pop(parts[-1], None)
    else:
        target[parts[-1]] = value


_STUDENT_REJECTIONS = _invalid_check_cases("verification", "student", "student_answer_free") + [
    ("verification.status", status) for status in ("unknown", "candidate", "quarantined", "verified", _MISSING)
] + [("edition.kind", "recollection"), ("edition.kind", _MISSING), ("edition", "unknown"),
     ("rights.status", "unresolved"), ("rights.status", "blocked"), ("rights.status", "licensed"),
     ("rights.decision.scope", "local_qa"), ("rights.decision.basis", _MISSING)]


@pytest.mark.parametrize("field,value", _STUDENT_REJECTIONS)
def test_purchased_student_rejection_matrix_is_http_fail_closed(tmp_path, monkeypatch, field, value):
    paper = _purchased_paper()
    _change_field(paper, field, value)
    _install_purchased(tmp_path, monkeypatch, paper)
    assert library.validate_catalog({"schema_version": 1, "papers": [paper]})  # shape alone is insufficient
    assert not library.is_available(paper), field
    client = _client(_User())
    listing = client.get("/api/past-papers").json()
    assert listing["library_ready"] and listing["total"] == 0 and listing["items"] == []
    assert listing["facets"] == {"years": [], "regions": [], "series": []}
    for suffix in ("", "/download/student", "/download/answer", "/preview/student/1", "/preview/answer/1"):
        assert client.get("/api/past-papers/gk2025-qg1" + suffix).status_code == 404, field


_ANSWER_REJECTIONS = _invalid_check_cases("answer", "answer", "pairing_confirmed") + [
    ("answer.status", status) for status in ("unknown", "candidate", "verified", "unavailable", _MISSING)
] + [("answer.paired_paper_id", "gk2025-qg2"), ("answer.paired_paper_id", _MISSING),
     ("answer.paired_student_sha256", "0" * 64), ("answer.paired_student_sha256", _MISSING),
     ("answer.kind", "unknown")]


@pytest.mark.parametrize("field,value", _ANSWER_REJECTIONS)
def test_purchased_answer_rejection_never_blocks_or_leaks_student(tmp_path, monkeypatch, field, value):
    paper = _purchased_paper()
    _change_field(paper, field, value)
    _install_purchased(tmp_path, monkeypatch, paper)
    assert library.is_available(paper) and not library.answer_available(paper), field
    client = _client(_User())
    listing = client.get("/api/past-papers").json()
    assert listing["total"] == 1
    detail_response = client.get("/api/past-papers/gk2025-qg1")
    assert detail_response.status_code == 200
    for payload in (listing["items"][0], detail_response.json()):
        assert payload["answer_available"] is False
        assert payload["answer"]["available"] is False
        assert payload["answer"]["kind"] == "" and payload["answer"]["note"] == ""
        assert payload["answer"]["checks"] == {} and payload["answer"]["preview_pages"] == 0
    assert client.get("/api/past-papers/gk2025-qg1/download/student").content == PDF_BYTES
    for suffix in ("/download/answer", "/preview/answer/1"):
        assert client.get("/api/past-papers/gk2025-qg1" + suffix).status_code == 404, field


def test_purchased_missing_answer_and_unknown_optional_counts_are_safe(tmp_path, monkeypatch):
    paper = _purchased_paper()
    paper["files"].pop("answer")
    paper.pop("answer")
    paper["verification"]["checks"]["evidence_path"] = "C:/private/synthetic-receipt.json"
    _install_purchased(tmp_path, monkeypatch, paper)
    client = _client(_User())
    listing = client.get("/api/past-papers").json()
    item = listing["items"][0]
    assert listing["total"] == 1 and item["question_count"] is None and item["total_points"] is None
    assert item["answer_available"] is False and "evidence_path" not in item["verification"]["checks"]
    assert client.get("/api/past-papers/gk2025-qg1/download/student").content == PDF_BYTES
    assert client.get("/api/past-papers/gk2025-qg1/download/answer").status_code == 404


def _generated_answer(paper):
    """Swap the source-paired answer for an offline model-solved key bound to the same files."""
    entry = paper["files"]["answer"]
    paper["answer"] = {
        "status": "generated", "method": library.GENERATED_ANSWER_METHOD, "kind": "answers_and_solutions",
        "note": "AI 解答生成的参考答案（含简要解析）。", "paired_paper_id": paper["paper_id"],
        "paired_student_sha256": paper["files"]["student"]["sha256"],
        "checks": {"all_pages_rendered": True, "every_question_addressed": True, "programmatic_recheck_completed": True,
                   "blocking_issue_count": 0, "file_sha256": entry["sha256"], "rendered_pages": entry["pages"]},
    }
    return paper


def test_generated_answer_is_its_own_status_through_http(tmp_path, monkeypatch):
    paper = _generated_answer(_purchased_paper())
    _install_purchased(tmp_path, monkeypatch, paper)
    assert library.answer_available(paper)
    client = _client(_User())
    item = client.get("/api/past-papers").json()["items"][0]
    detail = client.get("/api/past-papers/gk2025-qg1").json()
    for payload in (item, detail):
        assert payload["answer_available"] is True
        assert payload["answer"]["status"] == "generated"
        assert payload["answer"]["method"] == library.GENERATED_ANSWER_METHOD
        assert payload["answer"]["checks"]["programmatic_recheck_completed"] is True
    assert client.get("/api/past-papers/gk2025-qg1/download/answer").content == PDF_BYTES + b"source answer"
    assert client.get("/api/past-papers/gk2025-qg1/preview/answer/1").status_code == 200


_GENERATED_REJECTIONS = [
    ("answer.method", library.RESOURCE_CHECK_METHOD), ("answer.kind", "unknown"),
    ("answer.paired_paper_id", "gk2025-qg2"), ("answer.paired_student_sha256", "0" * 64),
    ("answer.checks.programmatic_recheck_completed", False), ("answer.checks.every_question_addressed", "true"),
    ("answer.checks.all_pages_rendered", None), ("answer.checks.blocking_issue_count", 1),
    ("answer.checks.file_sha256", "f" * 64), ("answer.checks.rendered_pages", 2),
    ("files.answer.role", "student"), ("files.answer.page_dimensions_mm", []),
]


@pytest.mark.parametrize("field,value", _GENERATED_REJECTIONS)
def test_generated_answer_rejections_fail_closed(tmp_path, monkeypatch, field, value):
    paper = _generated_answer(_purchased_paper())
    _change_field(paper, field, value)
    _install_purchased(tmp_path, monkeypatch, paper)
    assert library.is_available(paper) and not library.answer_available(paper), field
    client = _client(_User())
    assert client.get("/api/past-papers/gk2025-qg1").json()["answer"]["available"] is False
    assert client.get("/api/past-papers/gk2025-qg1/download/answer").status_code == 404, field


def test_generated_answer_does_not_borrow_or_lend_other_statuses():
    paired = _purchased_paper()
    paired["answer"]["method"] = library.GENERATED_ANSWER_METHOD
    assert not library.answer_available(paired)  # source_paired cannot use the generated contract
    # A legacy verified paper may carry a generated key, but still never a source-paired one.
    legacy = _generated_answer(_purchased_paper())
    legacy["edition"] = {"kind": "retypeset", "label": "忠实重排版"}
    legacy["verification"] = {"status": "verified", "method": library.LEGACY_VERIFICATION_METHOD}
    assert library.is_available(legacy) and library.answer_available(legacy)
    assert library.summary_payload(legacy)["answer"]["status"] == "generated"
    legacy_paired = _purchased_paper()
    legacy_paired["verification"] = {"status": "verified", "method": library.LEGACY_VERIFICATION_METHOD}
    assert not library.answer_available(legacy_paired)


def test_verified_never_borrows_purchased_method(built):
    papers, error = library.load_catalog()
    assert error is None
    for paper in papers.values():
        assert library.is_available(paper)
        before = json.loads(json.dumps(paper))
        for payload in (library.summary_payload(paper), library.detail_payload(paper)):
            assert payload["verification"]["status"] == "verified"
            assert payload["verification"]["method"] == "multi_lineage_majority_with_independent_review"
            assert payload["student"]["page_size"] == "A4"
            assert payload["edition"] == paper["edition"]
        assert paper == before  # serialization never rewrites legacy records
        bad = json.loads(json.dumps(paper))
        bad["verification"]["method"] = "purchased_pdf_identity_and_mechanical_check"
        assert not library.is_available(bad) and not library.answer_available(bad)


def test_invalid_catalog_fails_closed(tmp_path, monkeypatch):
    out = tmp_path / "broken"
    out.mkdir()
    (out / "catalog.json").write_text('{"schema_version": 1, "papers": [{"paper_id": "../x"}]}', encoding="utf-8")
    monkeypatch.setenv("PAST_PAPER_LIBRARY_DIR", str(out))
    data = _client(_User()).get("/api/past-papers").json()
    assert data["items"] == [] and data["library_ready"] is False
