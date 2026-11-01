"""题库选题服务 — 按条件筛选题目。"""

import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from models import QuestionBank
from services.math_curriculum import get_math_catalog
from services.teaching_context import current_teaching_context, normalize_education_stage

logger = logging.getLogger(__name__)


def load_math_catalog(education_stage: str | None = None) -> list[dict]:
    """Current tasks use a versioned catalog; only legacy calls use old labels."""
    stage = education_stage or current_teaching_context().get("education_stage")
    if stage:
        return get_math_catalog(stage)
    path = Path(__file__).resolve().parent.parent / "references" / "chapters_topics.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_knowledge_points(education_stage: str | None = None) -> dict:
    """Expose the single versioned catalog in the existing tree response shape."""
    stage = education_stage or current_teaching_context().get("education_stage")
    if not stage:
        return _default_knowledge_points()  # Explicit legacy caller, not a new task.
    return {"数学": {
        chapter["chapter"]: {topic: [] for topic in chapter["topics"]}
        for chapter in get_math_catalog(stage)
    }}


def filter_bank_stage(query, education_stage: str | None = None):
    """Unknown historical rows never become candidates for a new stage task."""
    return filter_teaching_stage(query, QuestionBank, education_stage)


def filter_teaching_stage(query, model, education_stage: str | None = None):
    """Apply exact stage scoping before pagination, without inferring legacy rows."""
    stage = education_stage or current_teaching_context().get("education_stage")
    if not stage:
        return query
    stage = normalize_education_stage(stage)
    column = getattr(model, "education_stage", None)
    if column is None:
        from sqlalchemy import false

        return query.filter(false())
    return query.filter(column == stage)


def _default_knowledge_points() -> dict:
    return {
        "数学": {
            "代数与函数": {
                "集合": ["子集", "交集", "并集", "补集"],
                "函数概念": ["定义域", "值域", "单调性", "奇偶性"],
                "二次函数": ["顶点", "对称轴", "最值", "根"],
                "指数函数": ["指数运算", "指数方程"],
                "对数函数": ["对数运算", "对数方程"],
                "幂函数": ["幂运算"],
            },
            "三角函数": {
                "三角比": ["正弦", "余弦", "正切"],
                "诱导公式": ["sin(π+θ)", "cos(π/2-θ)"],
                "和差公式": ["sin(α+β)", "cos(α-β)"],
                "辅助角公式": ["asinθ+bcosθ"],
                "三角方程": ["sinx=a", "cosx=a"],
            },
            "数列": {
                "等差数列": ["通项", "求和"],
                "等比数列": ["通项", "求和"],
                "递推数列": ["通项求法"],
                "数列求和": ["裂项法", "错位相减法"],
            },
            "平面向量": {
                "向量运算": ["加法", "减法", "数乘"],
                "数量积": ["点积", "夹角"],
                "向量坐标": ["坐标运算"],
            },
            "解析几何": {
                "直线": ["斜率", "方程", "距离"],
                "圆": ["标准方程", "一般方程"],
                "椭圆": ["方程", "离心率"],
                "双曲线": ["方程", "渐近线"],
                "抛物线": ["方程", "焦点"],
            },
            "立体几何": {
                "线面关系": ["平行", "垂直"],
                "体积": ["棱柱", "棱锥", "球"],
                "表面积": ["柱体", "锥体", "球"],
            },
            "概率与统计": {
                "古典概型": ["排列组合"],
                "条件概率": ["贝叶斯公式"],
                "随机变量": ["期望", "方差"],
                "统计图表": ["频率分布", "直方图"],
            },
            "导数": {
                "导数定义": ["极限"],
                "求导法则": ["基本公式", "链式法则"],
                "切线": ["切线方程"],
                "极值": ["单调性", "最值"],
            },
        }
    }


def select_questions(
    db: Session,
    chapter: Optional[str] = None,
    topic: Optional[str] = None,
    difficulty_min: Optional[int] = None,
    difficulty_max: Optional[int] = None,
    limit: int = 10,
    subject: Optional[str] = None,
    teacher_id: Optional[int] = None,
    education_stage: Optional[str] = None,
) -> list[dict]:
    """Select questions matching criteria.

    teacher_id: 限定到该老师可见范围（自己私库 + 公库，P0-4）。
    """
    q = filter_bank_stage(db.query(QuestionBank), education_stage)
    if teacher_id is not None:
        from services.bank_scope import visible_filter
        q = q.filter(visible_filter(teacher_id))
    conditions = []
    if subject:
        conditions.append(QuestionBank.subject == subject)
    if chapter:
        from services.security import escape_like
        conditions.append(QuestionBank.chapter.like(f"%{escape_like(chapter)}%", escape="\\"))
    if topic:
        from services.security import escape_like
        conditions.append(QuestionBank.topic.like(f"%{escape_like(topic)}%", escape="\\"))
    if difficulty_min is not None:
        conditions.append(QuestionBank.difficulty >= difficulty_min)
    if difficulty_max is not None:
        conditions.append(QuestionBank.difficulty <= difficulty_max)
    if conditions:
        q = q.filter(and_(*conditions))
    questions = q.order_by(QuestionBank.difficulty).limit(limit).all()
    return [q.to_dict() for q in questions]


def get_bank_statistics(db: Session, teacher_id: Optional[int] = None, education_stage: Optional[str] = None) -> dict:
    """Get question bank statistics by topic and difficulty using SQL aggregation.

    teacher_id: 限定到该老师可见范围（P0-4）。
    """
    from services.bank_scope import visible_filter
    scope = visible_filter(teacher_id) if teacher_id is not None else None

    def _q(*cols):
        qq = filter_bank_stage(db.query(*cols), education_stage)
        if scope is not None:
            qq = qq.filter(scope)
        return qq

    total = _q(func.count(QuestionBank.id)).scalar()

    by_subject = dict(
        _q(QuestionBank.subject, func.count())
        .group_by(QuestionBank.subject).all()
    )
    by_chapter = dict(
        _q(QuestionBank.chapter, func.count())
        .group_by(QuestionBank.chapter).all()
    )
    by_topic = dict(
        _q(QuestionBank.topic, func.count())
        .group_by(QuestionBank.topic).all()
    )
    by_difficulty = {
        str(k): v for k, v in
        _q(QuestionBank.difficulty, func.count())
        .group_by(QuestionBank.difficulty).all()
    }

    return {
        "total": total,
        "by_subject": by_subject,
        "by_chapter": by_chapter,
        "by_topic": by_topic,
        "by_difficulty": by_difficulty,
    }
