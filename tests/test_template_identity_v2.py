"""Upstream identities survive skipping choice text and all later consumers."""
import copy
import pytest
from services import template_directory_pipeline as pipeline
from services import template_one_three as contract
from tests import legacy_template_hierarchy as legacy


def directory(counts=(1, 1, 5, 3)):
    return {"contract_version": 2, "questions": [
        {"question_number": str(i), "printed_label": f"第{i}题", "cue": f"印刷开头{i}",
         "answer_regions": ["answer"] * n} for i, n in enumerate(counts, 1)]}


def test_active_hierarchy_prompt_describes_section_vs_question():
    prompt = pipeline._HIERARCHY_PROMPT
    assert '"contract_version":4' in prompt
    assert "同题多个作答位列在slots内" in prompt
    assert "printed_path" in prompt and "subquestions" not in prompt
    assert all(field in prompt for field in ("slots", "slot_id", "cue"))
    assert "才建立作图槽位" in prompt
    assert "看图回答" in prompt
    assert all(field not in prompt for field in ("answer_regions", "bbox"))


def test_v2_all_three_prompts_share_visible_identity():
    h = legacy._validate_flash_question_groups(directory())
    solver, text = contract.build_prompts(h, education_stage="primary")
    locator, _ = pipeline._parallel_template_prompts(h, width=1000, height=1400, columns=[])
    for prompt in (solver, text, locator):
        assert '"printed_label":"第3题"' in prompt
        assert '"cue":"印刷开头3"' in prompt
    assert "选择题保留该q_id" in text
    assert "不重编号" in text


def test_v2_transcription_keeps_choice_rows_empty_without_renumbering():
    h = legacy._validate_flash_question_groups(directory())
    payload = {"questions": [{"q_id": f"Q{i}", "question_text": "" if i < 3 else f"印刷开头{i}", "uncertain": []} for i in range(1, 5)]}
    assert contract.receive_transcription(payload, h) == payload
    payload["questions"].pop(0)
    with pytest.raises(ValueError, match="缺失"):
        contract.receive_transcription(payload, h)


def test_v2_rejects_multiple_independent_choices_in_one_q():
    from tests.test_template_one_three import branch_payloads
    h = legacy._validate_flash_question_groups(directory((2,)))
    raw = branch_payloads(h, choice=True)["template-directory-v1"]
    with pytest.raises(ValueError, match="独立选择题"):
        pipeline._validate_one_three_locator(raw, h)
