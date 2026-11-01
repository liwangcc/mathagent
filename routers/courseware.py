"""课件库：学段隔离、搜索、预览与离线 HTML 下载。"""

from __future__ import annotations

import os
from typing import Any, Optional
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models_courseware import Courseware
from models_user import User
from routers.auth import _require_user
from services.courseware import (
    bundled_courseware_for_stage,
    build_demo_html,
    courseware_directory,
    courseware_cover_metadata,
    demo_courseware_for_stage,
    safe_courseware_name,
    write_html_file,
)
from services.security import escape_like
from services.teaching_context import require_teacher_stage

router = APIRouter()


def _courseware_payload(row: Courseware) -> dict[str, Any]:
    payload = row.to_dict()
    payload.update(courseware_cover_metadata(
        chapter=row.chapter,
        knowledge_point=row.knowledge_point,
        stored_path=row.stored_path,
    ))
    return payload


class CoursewareCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    chapter: str = Field("", max_length=120)
    knowledge_point: str = Field("", max_length=160)
    summary: str = Field("", max_length=500)
    objective: str = Field("", max_length=2000)
    misconception: str = Field("", max_length=2000)
    steps: list[Any] = Field(default_factory=list, max_length=32)
    interaction_state: dict[str, Any] = Field(default_factory=dict)
    checkpoints: list[Any] = Field(default_factory=list, max_length=32)
    interaction_type: str = Field("custom", max_length=64)
    offline_file_name: str = Field("", max_length=240)
    content_html: str = Field(..., min_length=20, max_length=2_000_000)


def _visible_query(db: Session, user: User):
    stage = require_teacher_stage(user)
    return db.query(Courseware).filter(
        Courseware.education_stage == stage,
        or_(Courseware.is_public.is_(True), Courseware.teacher_id == user.id),
    )


def _ensure_demo(db: Session, user: User) -> None:
    """首次打开课件库时准备三个可直接验收的示例。"""
    stage = require_teacher_stage(user)
    existing = {
        (row.chapter or "", row.title or ""): row
        for row in db.query(Courseware).filter(
            Courseware.education_stage == stage,
            Courseware.is_public.is_(True),
        ).all()
    }
    changed = False
    for item in bundled_courseware_for_stage(stage):
        chapter = str(item.get("chapter") or "").strip()
        title = str(item.get("title") or chapter).strip()
        key = (chapter, title)
        if key in existing:
            continue
        file_name = str(item.get("offlineFileName") or os.path.basename(item.get("stored_path") or "课件.html"))
        db.add(Courseware(
            education_stage=stage,
            title=title,
            chapter=chapter,
            knowledge_point=str(item.get("knowledgePoint") or "").strip(),
            summary="v4 完整章节交互课件，可离线打开",
            objective=f"围绕{chapter}观察、操作并解释章节中的数学规律",
            misconception="把观察视图的变化误认为数学量本身发生了变化",
            steps=json.dumps([
                {"title": "观察问题"}, {"title": "操作参数"},
                {"title": "看到变化"}, {"title": "解释规律"},
                {"title": "小练习与答案"},
            ], ensure_ascii=False),
            interaction_state=json.dumps({"source": "v4", "chapter_code": item.get("code", "")}, ensure_ascii=False),
            checkpoints=json.dumps(["图形、公式、数值由同一课件状态驱动"], ensure_ascii=False),
            interaction_type="bundled-v4",
            offline_file_name=file_name,
            file_name=file_name,
            stored_path=item["stored_path"],
            is_public=True,
        ))
        existing[key] = True
        changed = True
    for title, chapter, point, accent in demo_courseware_for_stage(stage):
        file_name = safe_courseware_name(chapter or point) + ".html"
        content = build_demo_html(title, chapter, point, accent)
        row = existing.get((chapter, title))
        if row is not None:
            # Refresh seeded files in place so a teacher never receives an old
            # broken demo after the interaction model has been fixed.
            needs_refresh = True
            if os.path.isfile(row.stored_path):
                try:
                    with open(row.stored_path, "r", encoding="utf-8") as handle:
                        needs_refresh = handle.read() != content
                except OSError:
                    needs_refresh = True
            expected_dir = os.path.abspath(courseware_directory(stage, chapter))
            stored_abs = os.path.abspath(row.stored_path or "")
            try:
                in_expected_dir = os.path.commonpath([stored_abs, expected_dir]) == expected_dir
            except ValueError:
                in_expected_dir = False
            if not os.path.isfile(row.stored_path) or not in_expected_dir:
                row.stored_path = write_html_file(file_name, content, education_stage=stage, chapter=chapter)
                changed = True
            elif needs_refresh:
                with open(row.stored_path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                changed = True
            if row.file_name != file_name or row.offline_file_name != file_name or row.mime != "text/html":
                row.file_name = file_name
                row.offline_file_name = file_name
                row.mime = "text/html"
                changed = True
            continue
        path = write_html_file(file_name, content, education_stage=stage, chapter=chapter)
        db.add(Courseware(
            education_stage=stage,
            title=title,
            chapter=chapter,
            knowledge_point=point,
            summary="可离线双击打开的交互演示课件",
            objective=f"理解{point or chapter}的核心规律",
            misconception="把图形变化与公式变化割裂开来",
            steps=json.dumps([{"title": "观察问题"}, {"title": "操作参数"}, {"title": "解释规律"}, {"title": "小练习与答案"}], ensure_ascii=False),
            interaction_state="{}",
            checkpoints="[]",
            interaction_type={"分数的认识与运算": "fraction", "函数基础与一次函数": "linear", "三角函数与恒等变换": "unit-circle", "长度、周长与面积": "area", "二次函数": "quadratic", "图形变换与坐标": "transformations", "平面向量及其应用": "vectors", "立体图形": "solid-geometry", "空间几何关系": "space-relations", "简单几何图形与测量": "views-3d", "统计与数据分析": "statistics", "导数与函数变化率": "derivative"}.get(chapter, "custom"),
            offline_file_name=file_name,
            file_name=file_name,
            stored_path=path,
            is_public=True,
        ))
        changed = True
    if changed:
        db.commit()


@router.get("/courseware")
def list_courseware(
    q: Optional[str] = None,
    chapter: Optional[str] = None,
    knowledge_point: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    _ensure_demo(db, user)
    query = _visible_query(db, user)
    terms = [value.strip() for value in (q, chapter, knowledge_point) if value and value.strip()]
    for term in terms:
        pattern = f"%{escape_like(term)}%"
        query = query.filter(or_(Courseware.title.like(pattern, escape="\\"), Courseware.chapter.like(pattern, escape="\\"), Courseware.knowledge_point.like(pattern, escape="\\")))
    rows = query.order_by(Courseware.chapter, Courseware.title).limit(max(1, min(limit, 100))).all()
    return {"items": [_courseware_payload(row) for row in rows], "total": len(rows), "education_stage": require_teacher_stage(user)}


@router.get("/courseware/recommendations")
def recommend_courseware(
    chapter: Optional[str] = None,
    knowledge_point: Optional[str] = None,
    limit: int = 3,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """按批阅命中的知识点/章节返回最多三个课件。

    Recommendations are deliberately conservative: an exact knowledge-point
    hit outranks an exact chapter hit, and an empty/unmatched query returns no
    rows rather than presenting a generic courseware list as a recommendation.
    """
    _ensure_demo(db, user)
    chapter_term = (chapter or "").strip().lower()
    point_term = (knowledge_point or "").strip().lower()
    candidates = _visible_query(db, user).all()

    def match(row: Courseware) -> tuple[int, str, str, str]:
        row_chapter = (row.chapter or "").strip().casefold()
        row_point = (row.knowledge_point or "").strip().casefold()
        # Exact knowledge-point matches always win, even when chapter differs.
        # An empty row field is a substring of every query; it must never match.
        if point_term and row_point and row_point == point_term:
            return 4, "knowledge_point", row.knowledge_point or "", row.chapter or ""
        if point_term and row_point and (point_term in row_point or row_point in point_term):
            return 3, "knowledge_point", row.knowledge_point or "", row.chapter or ""
        if chapter_term and row_chapter and row_chapter == chapter_term:
            return 2, "chapter", row.knowledge_point or "", row.chapter or ""
        if chapter_term and row_chapter and (chapter_term in row_chapter or row_chapter in chapter_term):
            return 1, "chapter", row.knowledge_point or "", row.chapter or ""
        return 0, "", "", ""

    ranked = [(row, match(row)) for row in candidates]
    ranked = [item for item in ranked if item[1][0] > 0]
    ranked.sort(key=lambda item: (-item[1][0], (item[0].title or "").casefold(), item[0].id))
    cap = max(0, min(int(limit), 3))
    items = []
    for row, (priority, kind, matched_point, matched_chapter) in ranked[:cap]:
        payload = _courseware_payload(row)
        payload.update({
            "matched_knowledge_point": matched_point,
            "matched_chapter": matched_chapter,
            "match_type": kind,
            "recommendation_reason": (
                f"命中知识点：{matched_point}（章节：{matched_chapter}）"
                if kind == "knowledge_point"
                else f"命中章节：{matched_chapter}（知识点：{matched_point}）"
            ),
        })
        payload["match_reason"] = payload["recommendation_reason"]
        items.append(payload)
    return {
        "items": items,
        "total": len(items),
        "matched": bool(items),
        "message": "" if items else "暂无匹配课件",
    }


@router.get("/courseware/{courseware_id}")
def get_courseware(courseware_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user)):
    row = _visible_query(db, user).filter(Courseware.id == courseware_id).first()
    if not row:
        raise HTTPException(404, detail="课件不存在")
    return _courseware_payload(row)


@router.get("/courseware/{courseware_id}/download")
def download_courseware(courseware_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user)):
    row = _visible_query(db, user).filter(Courseware.id == courseware_id).first()
    if not row or not os.path.isfile(row.stored_path):
        raise HTTPException(404, detail="课件文件不存在")
    return FileResponse(row.stored_path, media_type=row.mime or "text/html", filename=row.file_name)


@router.get("/courseware/{courseware_id}/preview", response_class=HTMLResponse)
def preview_courseware(courseware_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user)):
    row = _visible_query(db, user).filter(Courseware.id == courseware_id).first()
    if not row or not os.path.isfile(row.stored_path):
        raise HTTPException(404, detail="课件文件不存在")
    with open(row.stored_path, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


@router.post("/courseware", status_code=201)
def create_courseware(payload: CoursewareCreateRequest, db: Session = Depends(get_db), user: User = Depends(_require_user)):
    stage = require_teacher_stage(user)
    chapter = payload.chapter.strip()
    point = payload.knowledge_point.strip()
    base_name = safe_courseware_name(chapter or point or payload.title)
    file_name = base_name + ".html"
    path = write_html_file(file_name, payload.content_html, education_stage=stage, chapter=chapter)
    # 离线文件名由章节/大知识点决定，避免上传者传入与教学范围无关的名称。
    offline_name = file_name
    row = Courseware(
        teacher_id=user.id, education_stage=stage, title=payload.title.strip(), chapter=chapter,
        knowledge_point=point, summary=payload.summary.strip(), objective=payload.objective.strip(),
        misconception=payload.misconception.strip(), steps=json.dumps(payload.steps, ensure_ascii=False),
        interaction_state=json.dumps(payload.interaction_state, ensure_ascii=False),
        checkpoints=json.dumps(payload.checkpoints, ensure_ascii=False),
        interaction_type=payload.interaction_type.strip() or "custom", file_name=file_name,
        offline_file_name=offline_name, stored_path=path, is_public=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()
