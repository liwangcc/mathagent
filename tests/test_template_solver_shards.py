"""Max full-solve shards: planning, strict per-shard receive, merge and pipeline wiring."""
import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

import config
from services import flash_grading
from services import template_directory_pipeline as pipeline
from services import template_one_three as contract
from services import template_solver_shards as shards
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.test_template_one_three import v4_payloads


def manifest_of(paths):
    return [{"q_id": f"Q{index}", "printed_path": list(path)} for index, path in enumerate(paths, 1)]


def numbered(count):
    return manifest_of([[f"{index}."] for index in range(1, count + 1)])


def test_sub_question_leaves_of_one_printed_question_form_one_group():
    manifest = manifest_of([
        ["一、选择", "1."], ["一、选择", "2."],
        ["计算", "3."], ["计算", "3.", "(1)"], ["计算", "3.", "(2)"],
        ["计算", "4.", "（1）"], ["计算", "4.", "（2）"],
        ["作图", "5.", "①"], ["作图", "5.", "②"],
        ["(1)"], ["(2)"],
    ])
    assert shards.question_groups(manifest) == [
        ["Q1"], ["Q2"], ["Q3", "Q4", "Q5"], ["Q6", "Q7"], ["Q8", "Q9"], ["Q10"], ["Q11"]]


def test_plan_is_longest_first_by_position_and_keeps_reading_order():
    assert shards.plan_shards(numbered(5), 2) == [["Q1", "Q2", "Q5"], ["Q3", "Q4"]]
    # Three shards leave the last (usually hardest) question alone.
    assert shards.plan_shards(numbered(5), 3) == [["Q1", "Q4"], ["Q2", "Q3"], ["Q5"]]
    plan = shards.plan_shards(numbered(19), 2)
    assert plan == [["Q1", "Q2", "Q5", "Q6", "Q9", "Q10", "Q13", "Q14", "Q17", "Q18"],
                    ["Q3", "Q4", "Q7", "Q8", "Q11", "Q12", "Q15", "Q16", "Q19"]]
    assert [sum(int(q[1:]) for q in shard) for shard in plan] == [95, 95]


def test_plan_never_splits_a_group_and_has_at_most_one_shard_per_group():
    manifest = manifest_of([["1."], ["2.", "(1)"], ["2.", "(2)"], ["2.", "(3)"], ["3."]])
    assert shards.plan_shards(manifest, 2) == [["Q1", "Q5"], ["Q2", "Q3", "Q4"]]
    assert shards.plan_shards(manifest, 4) == [["Q1"], ["Q2", "Q3", "Q4"], ["Q5"]]
    assert shards.plan_shards(numbered(1), 3) == [["Q1"]]
    assert shards.plan_shards(numbered(4), 1) == [["Q1", "Q2", "Q3", "Q4"]]


@pytest.mark.parametrize("configured, in_flight, expected", [
    (3, 4, 3), (3, 3, 2), (2, 2, 1), (3, 1, 1), (1, 8, 1), (4, 8, 4)])
def test_shard_count_leaves_one_max_permit_for_the_locator(monkeypatch, configured, in_flight, expected):
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", configured)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", in_flight)
    assert shards.configured_shard_count({"question_identity_version": 4}) == expected
    assert shards.configured_shard_count({"question_identity_version": 3}) == 1


def v4_contract(counts):
    raw, branches = v4_payloads(counts)
    return contract.question_contract(contract.receive_hierarchy(raw)), branches


def test_shard_receive_keeps_own_answers_and_counts_other_shards_answers():
    manifest, branches = v4_contract((1, 2, 1))
    result, dropped = shards.receive_shard(copy.deepcopy(branches[contract.SOLVE_PHASE]), manifest, ["Q1", "Q3"])
    assert [answer["q_id"] for answer in result["answers"]] == ["Q1", "Q3"]
    assert dropped == 1
    own_only = {"answers": [row for row in branches[contract.SOLVE_PHASE]["answers"] if row["q_id"] == "Q2"]}
    assert shards.receive_shard(own_only, manifest, ["Q2"])[1] == 0


@pytest.mark.parametrize("fault", ["missing_owned", "unknown", "duplicate_owned"])
def test_shard_receive_still_rejects_unbindable_owned_answers(fault):
    manifest, branches = v4_contract((1, 1, 1))
    payload = copy.deepcopy(branches[contract.SOLVE_PHASE])
    if fault == "missing_owned":
        payload["answers"] = [row for row in payload["answers"] if row["q_id"] != "Q3"]
    elif fault == "unknown":
        payload["answers"][0]["q_id"] = "Q9"
    else:
        payload["answers"].append(copy.deepcopy(payload["answers"][2]))
    with pytest.raises(ValueError):
        shards.receive_shard(payload, manifest, ["Q1", "Q3"])


def test_merge_rebuilds_single_request_order_and_rejects_gaps_or_overlap():
    manifest, branches = v4_contract((1, 1, 1))
    payload = branches[contract.SOLVE_PHASE]
    second, _ = shards.receive_shard(copy.deepcopy(payload), manifest, ["Q2"])
    first, _ = shards.receive_shard(copy.deepcopy(payload), manifest, ["Q1", "Q3"])
    merged = shards.merge_shard_results([second, first], manifest)
    assert [answer["q_id"] for answer in merged["answers"]] == ["Q1", "Q2", "Q3"]
    single = contract.max_owned.receive_solver_candidates(copy.deepcopy(payload), manifest)
    assert merged == single
    with pytest.raises(ValueError, match="覆盖"):
        shards.merge_shard_results([first], manifest)
    with pytest.raises(ValueError, match="重复"):
        shards.merge_shard_results([first, second, second], manifest)


def run_pipeline_with(tmp_path, monkeypatch, shard_count, counts=(1, 2, 1, 3, 1)):
    """Real _request and receivers; only the Flash and Max transports are faked."""
    from services import analyzer
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", shard_count)
    source = tmp_path / f"paper-{shard_count}.png"
    Image.new("RGB", (800, 1200), "white").save(source)
    raw_hierarchy, payloads = v4_payloads(counts)
    solver_calls = []

    async def flash(messages, **kwargs):
        prompt = messages[0]["content"][0]["text"]
        if prompt == pipeline._HIERARCHY_PROMPT:
            payload = raw_hierarchy
        elif prompt == pipeline._COARSE_LOCATOR_PROMPT:
            payload = {"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": str(i), "bbox": [0, (i - 1) * 200, 1000, i * 200]}
                for i in range(1, len(counts) + 1)]}
        else:
            payload = payloads[contract.TRANSCRIBE_PHASE]
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return {"content": raw, "model": "qwen3.8-flash", "receipt": {}}

    async def max_call(images, prompt, **kwargs):
        if "normalized_1000_v1" in prompt:
            payload = payloads["template-directory-v1"]
        else:
            solver_calls.append({"prompt": prompt, "images": list(images), "prompt_first": kwargs["prompt_first"]})
            payload = payloads[contract.SOLVE_PHASE]  # every shard answers everything
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return raw

    async def forbidden(*args, **kwargs):
        raise AssertionError("no real network")

    monkeypatch.setattr(flash_grading, "call_flash", flash)
    monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)
    monkeypatch.setattr(analyzer, "_call_api", forbidden)
    progress = {"stages": {}}

    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            with pipeline.template_import_context(asyncio.get_running_loop().time() + 60, progress):
                return await pipeline.recognize_directory_template(
                    str(source), template_mode="marked", layout_result={})

    return asyncio.run(run()), solver_calls, progress


def test_sharded_solve_produces_the_single_request_template(tmp_path, monkeypatch):
    single, single_calls, single_progress = run_pipeline_with(tmp_path, monkeypatch, 1)
    assert "error" not in single
    assert len(single_calls) == 1 and single_calls[0]["prompt_first"] is True
    assert contract.SOLVE_PHASE in single_progress["stages"]
    assert single["template_ocr_diagnostics"]["request_count"] == 5
    assert single["template_ocr_diagnostics"]["solver_shard_report"] == []
    base_prompt = single_calls[0]["prompt"]

    expected_plans = {2: [["Q1", "Q2", "Q5"], ["Q3", "Q4"]], 3: [["Q1", "Q4"], ["Q2", "Q3"], ["Q5"]]}
    for count, plan in expected_plans.items():
        result, calls, progress = run_pipeline_with(tmp_path, monkeypatch, count)
        assert "error" not in result
        assert result["questions"] == single["questions"]
        assert result["question_directory"] == single["question_directory"]
        assert result["template_answer_regions"] == single["template_answer_regions"]
        diagnostics = result["template_ocr_diagnostics"]
        assert diagnostics["solver_shards"] == count and diagnostics["solver_shard_plan"] == plan
        assert diagnostics["request_count"] == 4 + count
        assert diagnostics["solver_shard_report"] == [
            {"q_ids": owned, "out_of_scope_answers": 5 - len(owned)} for owned in plan]
        # Same images, same shared prompt, images first: only the tail differs.
        assert all(call["prompt"].startswith(base_prompt + "\n") for call in calls)
        tails = [call["prompt"][len(base_prompt):] for call in calls]
        assert sorted(tail.split("：", 1)[1].split("。", 1)[0] for tail in tails) == sorted(
            "、".join(owned) for owned in plan)
        assert all(len(tail) < 200 for tail in tails)
        assert sorted(call["prompt"] for call in calls) == sorted(
            shards.shard_prompt(base_prompt, owned) for owned in plan)
        assert all(call["images"] == calls[0]["images"] for call in calls)
        assert len(calls[0]["images"]) == len(single_calls[0]["images"])
        assert all(call["prompt_first"] is False for call in calls)
        stage_keys = {f"{contract.SOLVE_PHASE}#{index}" for index in range(1, count + 1)}
        assert stage_keys <= set(progress["stages"]) and contract.SOLVE_PHASE not in progress["stages"]
        assert all(progress["stages"][key]["status"] == "completed" for key in stage_keys)


def test_one_failed_shard_cancels_the_others_and_fails_like_one_request(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", 2)
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    raw_hierarchy, payloads = v4_payloads((1, 1, 1))
    cancelled = []

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == "template-hierarchy-v1":
            return validator(raw_hierarchy)
        if phase == "template-coarse-locator-v1":
            return validator({"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": str(i), "bbox": [0, 0, 1000, 1000]} for i in (1, 2, 3)]})
        if phase == contract.SOLVE_PHASE:
            if kwargs["stage_key"].endswith("#1"):
                await asyncio.sleep(0)
                raise ValueError("shard one failed")
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(kwargs["stage_key"])
                raise
        return validator(payloads[phase])

    monkeypatch.setattr(pipeline, "_request", request)

    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            result = await asyncio.wait_for(pipeline.recognize_directory_template(
                str(source), template_mode="marked", layout_result={}), 10)
        # Cancelled by the solve itself, not by the event loop shutting down.
        assert cancelled == [f"{contract.SOLVE_PHASE}#2"]
        return result

    result = asyncio.run(run())
    assert "shard one failed" in result["error"]
    assert result["questions"] == []


def test_image_first_ordering_is_refused_outside_the_solver():
    with pytest.raises(ValueError, match="solver-only"):
        asyncio.run(pipeline._request(["unused.png"], "prompt", contract.TRANSCRIBE_PHASE,
                                      lambda value: value, prompt_first=False))


@pytest.mark.parametrize("value", ["0", "5"])
def test_config_rejects_shard_counts_outside_one_to_four(tmp_path, value):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["OCR_TEMPLATE_SOLVER_SHARDS"] = value
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run([sys.executable, "-c", "import config"], cwd=tmp_path, env=env,
                               text=True, capture_output=True)
    assert completed.returncode != 0
    assert "OCR_TEMPLATE_SOLVER_SHARDS" in completed.stderr
