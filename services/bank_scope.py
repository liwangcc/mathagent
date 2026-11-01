"""题库归属可见性过滤（P0-4）。

归属模型：
  - 私库：teacher_id == 某老师，仅该老师可读写。
  - 公库：teacher_id IS NULL 且 is_public == True，全体老师可读用，仅 admin 可写。

集中在此，避免 30+ 处查询各写各的过滤条件而漏掉隔离。
"""

from sqlalchemy import or_, and_

from models import QuestionBank
from services.teaching_context import current_teaching_context


def teaching_filter():
    """Only new scoped work excludes legacy/other-stage catalog records."""
    context = current_teaching_context()
    if not context:
        return True
    return and_(
        QuestionBank.education_stage == context["education_stage"],
        QuestionBank.catalog_version == context["curriculum_version"],
    )


def visible_filter(teacher_id: int):
    """老师可见范围 = 自己的私库 + 公库。用于 query.filter(visible_filter(uid))。"""
    return and_(teaching_filter(), or_(
        QuestionBank.teacher_id == teacher_id,
        and_(QuestionBank.is_public.is_(True), QuestionBank.teacher_id.is_(None)),
    ))


def owned_filter(teacher_id: int):
    """老师可写范围 = 仅自己的私库。用于增删改前的归属校验。"""
    return QuestionBank.teacher_id == teacher_id


def public_filter():
    """公库范围 = teacher_id IS NULL 且 is_public=True。"""
    return and_(QuestionBank.teacher_id.is_(None), QuestionBank.is_public.is_(True))
