"""数学等价与部分正确由判分模型判断，代码只保留必要的输入/输出边界。"""

import json
import re

from services import analyzer, flash_grading
from services.text_grading_rules import TEXT_GRADING_DETAIL_INSTRUCTIONS, TEXT_GRADING_RULES


def test_shared_rules_delegate_equivalence_but_keep_ocr_tolerance():
    for rules in (TEXT_GRADING_RULES, TEXT_GRADING_RULES + "\n" + TEXT_GRADING_DETAIL_INSTRUCTIONS):
        assert "数学等价表达" in rules
        assert "OCR容错" in rules
        assert "中间极少量孤立的数字或符号偏差" in rules
        assert "系统数值归一" not in rules
        assert "等值核对未命中" not in rules
        assert "partial 只适用于" not in rules


def test_flash_text_rules_delegate_equivalence_but_keep_ocr_tolerance():
    assert "数学等价表达" in flash_grading._TEXT_RULES
    assert "OCR容错" in flash_grading._TEXT_RULES
    assert "中间极少量孤立的数字或符号偏差" in flash_grading._TEXT_RULES
    assert "系统数值归一" not in flash_grading._TEXT_RULES
    assert "numeric_evidence" not in flash_grading._TEXT_RULES


def test_text_payload_does_not_add_numeric_evidence():
    messages = flash_grading.build_text_messages([{
        "question_no": "1",
        "question_text": "把 3/5 写成小数。",
        "standard_answer": "0.6",
        "student_answer": "0.6",
    }])
    payload = json.loads(messages[1]["content"])
    assert payload["answers"] == [{"question_no": "1", "student_answer": "0.6"}]
    assert "numeric_evidence" not in json.dumps(messages, ensure_ascii=False)


def test_numeric_evidence_helper_is_removed():
    assert not hasattr(analyzer, "numeric_evidence")


def test_flash_prompt_version_tracks_rule_changes():
    assert re.fullmatch(r"flash-text-v\d+", flash_grading.FLASH_TEXT_PROMPT_VERSION)
    assert flash_grading.FLASH_TEXT_PROMPT_VERSION == "flash-text-v8"


def test_rules_limit_tolerance_to_working_with_an_already_correct_final_result():
    # Equivalence is delegated to the model. Tolerance may not repair the final
    # result or turn failure to qualify for tolerance into mandatory whole-item wrong.
    for rules in (TEXT_GRADING_RULES, flash_grading._TEXT_RULES):
        assert "接受数学等价表达和其他正确解法" in rules
        assert "同一所求答案的多步解答" in rules
        assert "学生写出的最终结果正确、主要思路和步骤大体成立" in rules
        assert "只有中间极少量孤立的数字或符号偏差" in rules
        assert "该答案按正确处理" in rules
        assert "还原后" not in rules
        assert "过程或结论" not in rules
        assert "明确错误判wrong" not in rules
        assert "needs_review" not in rules
