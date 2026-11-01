"""Synthetic replay of the reported coordinates; no uploads or model calls."""
import copy
import json

import pytest

from services.processing_errors import (
    TemplateSchemaError, describe_failure, failure_from_public, processing_stage,
)
from services.template_evidence import normalize_question_schema
from services.template_max_owned import locator_prompt, receive_locator


def schema(anchor):
    return {"question_number": "1", "question_type": "solve", "answer_kind": "mixed",
            "drawing_regions": [[249, 196, 567, 213]], "label_anchor": anchor}


def locator(anchor):
    manifest = [{"q_id": "Q1", "slots": [{"slot_id": "1[1]"}, {"slot_id": "1[2]"}]}]
    payload = {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [{
        "question_number": "1", "question_number_bbox": [20, 190, 40, 213],
        "question_type": "solve", "choice_mode": "unknown", "label_anchor": anchor,
        "regions": [{"slot_id": "1[1]", "kind": "drawing", "bbox": [249, 196, 567, 213]},
                    {"slot_id": "1[2]", "kind": "answer", "bbox": [249, 230, 567, 270]}],
    }]}
    return payload, manifest


@pytest.mark.parametrize("anchor", [[250, 205], [250, 200], [249.001, 205]])
def test_incident_replay_rejected_at_both_boundaries_with_actionable_public_error(anchor):
    row = schema(anchor)
    payload, manifest = locator(anchor)
    before = copy.deepcopy((row, payload))
    for call in (lambda: normalize_question_schema([row]), lambda: receive_locator(payload, manifest)):
        try:
            with processing_stage("template"):
                try:
                    call()
                except ValueError as original:
                    wrapper = RuntimeError("model response secret-canary")
                    wrapper.code = "TEMPLATE_OUTPUT_INVALID"
                    raise wrapper from original
        except RuntimeError as exc:
            info = describe_failure(exc, stage="template")
        else:
            pytest.fail("invalid geometry was accepted")
        assert info["code"] == "TEMPLATE_SCHEMA_INVALID"
        assert "第 1 题作图标签必须位于主作图区左侧，请教师复核" in info["message"]
        assert info["retryable"] is False
        assert "仅会重新生成" in info["action"] and "可能再次" in info["action"]
        assert "secret-canary" not in json.dumps(info)
        assert describe_failure(failure_from_public(json.loads(json.dumps(info)))) == info
    assert (row, payload) == before


@pytest.mark.parametrize("anchor", [[248, 205], [249, 205]])
def test_left_and_boundary_are_accepted_without_rewriting_coordinates(anchor):
    payload, manifest = locator(anchor)
    received = receive_locator(payload, manifest)["questions"][0]
    assert normalize_question_schema([received])[0]["label_anchor"] == anchor
    assert normalize_question_schema([schema(anchor)])[0]["drawing_regions"] == [[249, 196, 567, 213]]


@pytest.mark.parametrize("changes,rule", [
    ({"label_anchor": None}, "drawing_anchor_invalid"),
    ({"label_anchor": [float("nan"), 205]}, "drawing_anchor_invalid"),
    ({"label_anchor": [True, 205]}, "drawing_anchor_invalid"),
    ({"drawing_regions": [[249, 196, 249, 213]]}, "drawing_regions_invalid"),
    ({"drawing_regions": [[249, 196, 1001, 213]]}, "drawing_regions_invalid"),
    ({"answer_kind": "unknown"}, "answer_kind_invalid"),
])
def test_unknowable_geometry_keeps_failing_closed(changes, rule):
    with pytest.raises(TemplateSchemaError) as error:
        normalize_question_schema([{**schema([249, 205]), **changes}])
    info = describe_failure(error.value, stage="template")
    assert info["schema_issue"]["rule"] == rule
    assert "第 1 题" in info["message"]


def test_prompt_matches_frozen_left_boundary_contract():
    _, manifest = locator([249, 205])
    prompt = locator_prompt(manifest, width=1000, height=1000, columns=[])
    assert "x不得大于第一个作图框的左边界" in prompt
    assert "涂色和图文混合题也遵守" in prompt
    assert "不以它相对作图区" not in prompt


def test_public_rule_detail_does_not_leak_model_identity_or_arbitrary_exceptions():
    error = TemplateSchemaError("drawing_anchor_not_left", "student-secret-canary", question_index=2)
    info = describe_failure(error, stage="template")
    assert "第 2 个题目条目" in info["message"]
    assert "canary" not in json.dumps(info)
    generic = describe_failure(ValueError("第 1 题 secret-canary"), stage="template")
    assert generic["code"] == "OUTPUT_PARSE_FAILED" and generic["retryable"] is True
    assert "canary" not in json.dumps(generic)
    transient = describe_failure(TimeoutError(), stage="template")
    assert transient["retryable"] is True and "稍后重试" in transient["action"]
