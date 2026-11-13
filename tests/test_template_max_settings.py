"""What the deployment sets for the template Max route is what every Max request of a page uses and reports."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
from services import template_one_three as contract
from tests.test_template_locator_tiles import PHASE, run_pipeline_with


def max_stages(progress, phase):
    """The progress records of ``phase``'s requests: the single request, or one per tile or shard."""
    return {key: record for key, record in progress["stages"].items() if key.split("#")[0] == phase}


@pytest.mark.parametrize("tile_questions, solver_shards, locator_requests", [
    (0, 1, 1),   # one page request for each branch
    (3, 4, 4),   # four locator tiles and four solver shards
])
@pytest.mark.parametrize("locator_effort, solver_effort", [("low", "medium"), ("high", "xhigh")])
def test_every_max_request_uses_the_configured_model_and_its_branchs_effort(
        tmp_path, monkeypatch, tile_questions, solver_shards, locator_requests, locator_effort, solver_effort):
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen4-max-preview")
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_REASONING_EFFORT", locator_effort)
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_REASONING_EFFORT", solver_effort)

    result, _, progress = run_pipeline_with(tmp_path, monkeypatch, tile_questions,
                                            solver_shards=solver_shards, in_flight=8)

    assert "error" not in result
    locator, solver = max_stages(progress, PHASE), max_stages(progress, contract.SOLVE_PHASE)
    assert (len(locator), len(solver)) == (locator_requests, solver_shards)
    assert {(record["model"], record["reasoning_effort"]) for record in locator.values()} == {
        ("qwen4-max-preview", locator_effort)}
    assert {(record["model"], record["reasoning_effort"]) for record in solver.values()} == {
        ("qwen4-max-preview", solver_effort)}
    diagnostics = result["template_ocr_diagnostics"]
    assert diagnostics["model"] == diagnostics["solver_model"] == "qwen4-max-preview"
    assert diagnostics["hierarchy_model"] == diagnostics["transcription_model"] == "qwen3.8-flash"
    assert (diagnostics["max_reasoning_effort"], diagnostics["solver_reasoning_effort"]) == (
        locator_effort, solver_effort)
    # The Flash stages keep their own tiers.
    assert (diagnostics["hierarchy_reasoning_effort"], diagnostics["coarse_locator_reasoning_effort"],
            diagnostics["transcription_reasoning_effort"]) == ("low", "low", "high")


def load_config(tmp_path, **settings):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in ("OCR_TEMPLATE_LOCATOR_REASONING_EFFORT", "OCR_TEMPLATE_SOLVER_REASONING_EFFORT"):
        env.pop(name, None)
    env.update(settings)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import config; print(config.OCR_TEMPLATE_LOCATOR_REASONING_EFFORT, config.OCR_TEMPLATE_SOLVER_REASONING_EFFORT)"],
        cwd=tmp_path, env=env, text=True, encoding="utf-8", capture_output=True)


def test_the_locator_thinks_at_low_and_the_solver_at_medium_unless_the_deployment_says_otherwise(tmp_path):
    assert load_config(tmp_path).stdout.strip() == "low medium"
    adjusted = load_config(tmp_path, OCR_TEMPLATE_LOCATOR_REASONING_EFFORT=" High ",
                           OCR_TEMPLATE_SOLVER_REASONING_EFFORT="high")
    assert adjusted.returncode == 0, adjusted.stderr
    assert adjusted.stdout.strip() == "high high"


@pytest.mark.parametrize("name", ["OCR_TEMPLATE_LOCATOR_REASONING_EFFORT", "OCR_TEMPLATE_SOLVER_REASONING_EFFORT"])
def test_config_rejects_an_effort_the_provider_does_not_know(tmp_path, name):
    completed = load_config(tmp_path, **{name: "extreme"})
    assert completed.returncode != 0
    assert name in completed.stderr
