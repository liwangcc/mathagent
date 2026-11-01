"""Pure school-stage tests; no production configuration or model requests."""
import asyncio
from types import SimpleNamespace

import pytest

from services.teaching_context import (
    current_teaching_context,
    make_teaching_context,
    normalize_education_stage,
    require_teacher_stage,
    stage_prompt,
    use_teaching_context,
    validate_teaching_context,
)


@pytest.mark.parametrize("value", [None, "", "university", "高中", " junior ", True, 1, []])
def test_stage_is_explicit_not_inferred(value):
    with pytest.raises(ValueError):
        normalize_education_stage(value)


def test_only_stored_legacy_may_be_empty():
    assert normalize_education_stage(None, allow_legacy=True) == ""
    assert validate_teaching_context(None) == {}
    with pytest.raises(ValueError):
        validate_teaching_context({}, allow_legacy=False)


def test_profile_required_for_new_teaching_work():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        require_teacher_stage(SimpleNamespace(role="teacher"))
    assert caught.value.status_code == 428
    assert caught.value.detail["code"] == "teaching_profile_required"
    assert require_teacher_stage(SimpleNamespace(education_stage="junior")) == "junior"


def test_context_binds_stage_and_versions():
    junior = make_teaching_context("junior")
    senior = make_teaching_context("senior")
    assert junior != senior
    assert validate_teaching_context(junior) == junior
    for field in ("curriculum_version", "rules_version"):
        invalid = {**junior, field: "untrusted-or-obsolete-version"}
        with pytest.raises(ValueError):
            validate_teaching_context(invalid)


def test_legacy_prompt_unchanged_and_nested_stage_restored():
    original = "你是中国高中数学教师。"
    assert stage_prompt(original) == original
    with use_teaching_context(make_teaching_context("junior")):
        assert "中国初中数学教师" in stage_prompt(original)
        with use_teaching_context({}):
            assert stage_prompt(original) == original
        assert current_teaching_context()["education_stage"] == "junior"
        detached = current_teaching_context()
        detached["education_stage"] = "senior"
        assert current_teaching_context()["education_stage"] == "junior"
    assert current_teaching_context() == {}


def test_parallel_tasks_do_not_share_school_stage():
    async def run(stage):
        with use_teaching_context(make_teaching_context(stage)):
            await asyncio.sleep(0)
            return current_teaching_context()["education_stage"]

    async def main():
        return await asyncio.gather(run("junior"), run("senior"), run("junior"))

    assert asyncio.run(main()) == ["junior", "senior", "junior"]
    assert current_teaching_context() == {}


def test_exception_restores_outer_context():
    with pytest.raises(RuntimeError):
        with use_teaching_context(make_teaching_context("junior")):
            raise RuntimeError("simulated worker error")
    assert current_teaching_context() == {}


def test_sse_context_is_only_bound_during_production_and_close():
    from services.teaching_context import stream_teaching

    seen = []
    async def producer():
        try:
            seen.append(current_teaching_context())
            yield "first"
            seen.append(current_teaching_context())
            yield "second"
        finally:
            seen.append(current_teaching_context())

    async def run():
        context = make_teaching_context("junior")
        events = stream_teaching(context, producer())
        assert await anext(events) == "first"
        assert current_teaching_context() == {}
        await events.aclose()
        assert current_teaching_context() == {}
        assert seen == [context, context]

    asyncio.run(run())
