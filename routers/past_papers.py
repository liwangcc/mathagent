"""整卷真题：浏览、预览与下载经核验的完整高考数学试卷。

Read-only over the offline-built library (see services/past_papers.py).  All
routes require a logged-in user; the library holds senior-high Gaokao papers,
so it follows the bank's stage scoping: other stages get an explanatory empty
list instead of senior material.  Files are served by catalog identity only.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from models_user import User
from routers.auth import _require_user
from services import past_papers as library
from services.teaching_context import require_teacher_stage

router = APIRouter()
logger = logging.getLogger(__name__)

SUPPORTED_STAGE = "senior"
UNSUPPORTED_STAGE_MESSAGE = "整卷真题目前收录高考数学试卷，仅对高中学段开放；可在个人资料中切换学段。"


def _visible_papers(user: User) -> tuple[dict, str | None, bool]:
    stage = require_teacher_stage(user)
    if stage != SUPPORTED_STAGE:
        return {}, None, False
    papers, error = library.load_catalog()
    if error:
        # Logged with detail for operators; teachers only see a generic state.
        logger.error("past-paper library unavailable: %s", error)
    return papers, error, True


def _get_available(user: User, paper_id: str) -> dict:
    if not library.PAPER_ID_RE.fullmatch(paper_id or ""):
        raise HTTPException(404, detail="试卷不存在")
    papers, _, supported = _visible_papers(user)
    paper = papers.get(paper_id)
    if not supported or paper is None or not library.is_available(paper):
        raise HTTPException(404, detail="试卷不存在或暂未开放下载")
    return paper


def _matches(paper: dict, *, year, region, series_key, track, session, kind) -> bool:
    if year is not None and paper.get("year") != year:
        return False
    if region and region not in (paper.get("regions") or []):
        return False
    if series_key and paper.get("series_key") != series_key:
        return False
    if track:
        wanted = None if track == "none" else track
        if paper.get("track") != wanted:
            return False
    if session and paper.get("session") != session:
        return False
    if kind and library.SERIES[paper["series_key"]]["kind"] != kind:
        return False
    return True


def _facets(papers) -> dict:
    years = sorted({p["year"] for p in papers}, reverse=True)
    series = {}
    regions = {}
    for paper in papers:
        key = paper["series_key"]
        entry = series.setdefault(key, {"key": key, "kind": library.SERIES[key]["kind"], "names": set(),
                                         "years": set()})
        entry["names"].add(library.series_name(key, paper["year"]))
        entry["years"].add(paper["year"])
        for region in paper.get("regions") or []:
            regions.setdefault(region, set()).add(paper["year"])
    series_list = []
    for entry in series.values():
        years_of = sorted(entry["years"])
        series_list.append({
            "key": entry["key"], "kind": entry["kind"],
            "name": " / ".join(sorted(entry["names"])),
            "years": years_of[::-1],
            "label": f"{' / '.join(sorted(entry['names']))}（{years_of[0]}" +
                     (f"–{years_of[-1]}）" if years_of[-1] != years_of[0] else "）"),
        })
    series_list.sort(key=lambda s: (s["kind"] != "national", -max(s["years"]), s["key"]))
    region_list = [{"key": key, "name": library.REGION_NAMES[key], "years": sorted(years_, reverse=True)}
                   for key, years_ in regions.items()]
    order = [key for key, _ in library.REGIONS]
    region_list.sort(key=lambda r: order.index(r["key"]))
    return {"years": years, "series": series_list, "regions": region_list}


@router.get("/past-papers")
def list_past_papers(
    year: Optional[int] = None,
    region: Optional[str] = None,
    series_key: Optional[str] = None,
    track: Optional[str] = None,
    session: Optional[str] = None,
    kind: Optional[str] = None,
    user: User = Depends(_require_user),
):
    if region and region not in library.REGION_NAMES:
        raise HTTPException(422, detail="未知地区")
    if series_key and series_key not in library.SERIES:
        raise HTTPException(422, detail="未知卷型")
    if track and track not in (*library.TRACK_NAMES, "none"):
        raise HTTPException(422, detail="未知文理类型")
    if session and session not in library.SESSION_NAMES:
        raise HTTPException(422, detail="未知考试场次")
    if kind and kind not in ("national", "regional"):
        raise HTTPException(422, detail="未知卷型类别")
    papers, error, supported = _visible_papers(user)
    if not supported:
        return {"stage_supported": False, "message": UNSUPPORTED_STAGE_MESSAGE, "items": [], "total": 0,
                "facets": {"years": [], "series": [], "regions": []}}
    available = [p for p in papers.values() if library.is_available(p)]
    items = [p for p in available if _matches(p, year=year, region=region, series_key=series_key,
                                              track=track, session=session, kind=kind)]
    items.sort(key=lambda p: (-p["year"], p["session"] != "summer",
                              library.SERIES[p["series_key"]]["kind"] != "national", p["paper_id"]))
    payload = {
        "stage_supported": True,
        "library_ready": error is None,
        "items": [library.summary_payload(p) for p in items],
        "total": len(items),
        "facets": _facets(available),
    }
    if (user.role or "").lower() == "admin":
        withheld = [p for p in papers.values() if not library.is_available(p)]
        payload["withheld"] = {
            "rights_pending": sum(1 for p in withheld
                                  if (p.get("verification") or {}).get("status") == "verified"),
            "not_verified": sum(1 for p in withheld
                                if (p.get("verification") or {}).get("status") != "verified"),
        }
    return payload


@router.get("/past-papers/{paper_id}")
def get_past_paper(paper_id: str, user: User = Depends(_require_user)):
    return library.detail_payload(_get_available(user, paper_id))


def _serve(paper: dict, kind: str, *, preview_page: int | None = None):
    try:
        path, expected = library.resolve_file(paper, kind, preview_page=preview_page)
    except FileNotFoundError:
        missing = "暂无经核验的答案文件" if kind == "answer" else "试卷文件暂不可用"
        raise HTTPException(404, detail=missing)
    if expected is not None and not library.sha256_matches(path, expected):
        logger.error("past-paper file checksum mismatch: %s/%s", paper["paper_id"], kind)
        raise HTTPException(409, detail="试卷文件校验失败，已停止下载，请联系管理员")
    if preview_page is not None:
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=3600"})
    filename = library.download_filename(paper, kind)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{paper['paper_id']}-{kind}.pdf\"; "
                                   f"filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/past-papers/{paper_id}/download/{kind}")
def download_past_paper(paper_id: str, kind: str, user: User = Depends(_require_user)):
    if kind not in library.FILE_KINDS:
        raise HTTPException(404, detail="文件不存在")
    return _serve(_get_available(user, paper_id), kind)


@router.get("/past-papers/{paper_id}/preview/{kind}/{page}")
def preview_past_paper(paper_id: str, kind: str, page: int, user: User = Depends(_require_user)):
    if kind not in library.FILE_KINDS or page < 1 or page > 64:
        raise HTTPException(404, detail="预览页不存在")
    return _serve(_get_available(user, paper_id), kind, preview_page=page)
