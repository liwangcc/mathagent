"""What the deployment sets for the template Max route is what every Max request of a page uses and reports."""

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
def test_every_max_request_reports_the_configured_model(
        tmp_path, monkeypatch, tile_questions, solver_shards, locator_requests):
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen4-max-preview")

    result, _, progress = run_pipeline_with(tmp_path, monkeypatch, tile_questions,
                                            solver_shards=solver_shards, in_flight=8)

    assert "error" not in result
    locator, solver = max_stages(progress, PHASE), max_stages(progress, contract.SOLVE_PHASE)
    assert (len(locator), len(solver)) == (locator_requests, solver_shards)
    assert {record["model"] for record in [*locator.values(), *solver.values()]} == {"qwen4-max-preview"}
    diagnostics = result["template_ocr_diagnostics"]
    assert diagnostics["model"] == diagnostics["solver_model"] == "qwen4-max-preview"
    assert diagnostics["hierarchy_model"] == diagnostics["transcription_model"] == "qwen3.8-flash"
