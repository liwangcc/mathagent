"""Immutable mathematical school-stage context for new teaching work.

An empty context explicitly denotes legacy work. It must never be filled from
the teacher's current profile while resuming a saved task. This module does not
read user data, configuration, databases or model credentials.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


EDUCATION_STAGES = frozenset({"primary", "junior", "senior"})
STAGE_LABELS = {"primary": "小学", "junior": "初中", "senior": "高中"}
MATH_RULES_VERSION = "math-stage-v1"
_context: ContextVar[dict | None] = ContextVar("mathagent_teaching_context", default=None)


def normalize_education_stage(value: object, *, allow_legacy: bool = False) -> str:
    """Validate stored/API values; do not guess a stage from text or age."""
    if allow_legacy and (value is None or value == ""):
        return ""
    if not isinstance(value, str) or value not in EDUCATION_STAGES:
        raise ValueError("请选择小学、初中或高中学段")
    return value


def require_teacher_stage(user) -> str:
    """Backend gate for new teaching tasks, independent of frontend state."""
    from fastapi import HTTPException

    try:
        return normalize_education_stage(getattr(user, "education_stage", None))
    except ValueError as exc:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "teaching_profile_required",
                "message": "请先在个人资料中选择小学、初中或高中学段",
            },
        ) from exc


def make_teaching_context(stage: str) -> dict:
    from services.math_curriculum import catalog_version

    stage = normalize_education_stage(stage)
    return {
        "education_stage": stage,
        "curriculum_version": catalog_version(stage),
        "rules_version": MATH_RULES_VERSION,
    }


def validate_teaching_context(value: object, *, allow_legacy: bool = True) -> dict:
    """Validate a frozen snapshot without silently updating its versions."""
    if value is None or value == {}:
        if allow_legacy:
            return {}
        raise ValueError("教学任务缺少学段，请重新创建任务")
    if not isinstance(value, dict):
        raise ValueError("教学上下文格式错误")
    stage = normalize_education_stage(value.get("education_stage"))
    expected = make_teaching_context(stage)
    if value.get("rules_version") != expected["rules_version"]:
        raise ValueError("教学规则版本不受支持，请重新创建并确认任务")
    if value.get("curriculum_version") != expected["curriculum_version"]:
        raise ValueError("知识点目录版本不受支持，请重新创建并确认任务")
    return expected


def current_teaching_context() -> dict:
    return copy.deepcopy(_context.get() or {})


def record_teaching_fields(context: dict | None = None, *, chapter: str = "", topic: str = "") -> dict:
    """Fields for new knowledge-bank rows; explicit {} always stays legacy."""
    snapshot = current_teaching_context() if context is None else validate_teaching_context(context)
    if not snapshot:
        return {}
    from services.math_curriculum import get_knowledge_point
    point = get_knowledge_point(snapshot["education_stage"], chapter, topic)
    return {
        "education_stage": snapshot["education_stage"],
        "catalog_version": snapshot["curriculum_version"],
        "knowledge_point_id": point["id"] if point else "",
    }


@contextmanager
def use_teaching_context(context: dict | None) -> Iterator[dict]:
    """Scope parallel/async work and restore the caller even after failure."""
    snapshot = validate_teaching_context(context)
    token = _context.set(snapshot)
    try:
        yield copy.deepcopy(snapshot)
    finally:
        _context.reset(token)


def stage_label(context: dict | None = None, *, legacy: str = "高中") -> str:
    snapshot = current_teaching_context() if context is None else context
    if not snapshot:
        return legacy
    return STAGE_LABELS[normalize_education_stage(snapshot.get("education_stage"))]


def stage_prompt(template: str, context: dict | None = None) -> str:
    """Adapt a trusted prompt template BEFORE inserting user/source content.

    Never call this on student transcriptions, problem text, JSON payloads or
    complete conversations. Legacy prompts retain their original byte content.
    Model routing, image geometry, grading contracts and outputs are untouched.
    """
    snapshot = current_teaching_context() if context is None else context
    if not snapshot:
        return template
    label = stage_label(snapshot)
    result = template.replace("高中", label)
    # Stage-neutral helpers remain safe for legacy callers while live scoped
    # prompts still expose the concrete school stage to the model.
    if f"{label}数学批改助手" not in result:
        result = result.replace("数学批改助手", f"{label}数学批改助手")
    # The extra scope is needed for otherwise stage-neutral prompt templates.
    scope = (
        f"本次任务学段为{label}，学科为数学。"
        "按该学段教学语境理解题目，遵守题目要求及教师确认的评分规则。"
        "不得仅因学生使用其他数学上正确的方法而判错。"
    )
    # Keep task-marker prefixes intact: the existing scheduler uses them to
    # choose the blank-template/handwriting lane and its provider parameters.
    return f"{result}\n{scope}"


async def await_teaching(context: dict | None, operation):
    """Run an already-created coroutine inside its persisted task context."""
    with use_teaching_context(context):
        return await operation


async def stream_teaching(context: dict | None, events):
    """Bind producer work without leaking context across an SSE yield."""
    iterator = aiter(events)
    try:
        while True:
            try:
                with use_teaching_context(context):
                    event = await anext(iterator)
            except StopAsyncIteration:
                break
            yield event
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with use_teaching_context(context):
                await close()
