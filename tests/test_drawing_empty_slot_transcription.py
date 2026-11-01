"""Pure drawing has no textual slot; preserve that boundary explicitly."""
import json

import pytest

from services.ocr_service import _multi_pool_response_validator, validate_visual_transcriptions
from services.paper_cache import RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT
from services.template_slots import bind_student_slots, build_slot_evidence_prompt, slot_transcription_prompt


def drawing():
    return {"question_number": "5", "answer_kind": "drawing", "slot_ids": ["5[1]"], "region_kinds": ["drawing"]}


def test_pure_drawing_does_not_create_a_text_slot_contract():
    assert slot_transcription_prompt([drawing()]) == ""


def test_mixed_prompt_contract_only_lists_text_slots():
    questions = [{"question_no": "4", "slot_ids": ["4[1]"]}, drawing()]
    prompt = build_slot_evidence_prompt(RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT, questions)
    assert '"4":["4[1"]' not in prompt  # malformed contract must never be emitted
    assert '"4":["4[1]"]' in prompt
    assert '"5"' not in prompt.split("逐槽转录合同=", 1)[1]


def test_live_empty_drawing_response_crosses_transport_and_cache_boundaries():
    questions = [{"question_no": "4", "slot_ids": ["4[1]"]}, drawing()]
    prompt = (
        "[task=student_transcription_only]\nanswer_state student_answer\n"
        '作答类型={"4":"solve","5":"drawing"}\n'
        + build_slot_evidence_prompt(RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT, questions)
    )
    raw = {"questions": [
        {"question_number": "4", "student_slots": [{"slot_id": "4[1]", "student_answer": "第二位数没变", "answer_state": "visible"}]},
        {"question_number": "5", "student_slots": []},
    ]}
    before = json.dumps(raw, ensure_ascii=False)
    _multi_pool_response_validator(prompt)(before)
    normalized = validate_visual_transcriptions(
        raw["questions"],
        slot_contracts={"4": ["4[1]"]},
        drawing_only_numbers={"5"},
    )
    assert normalized[1]["student_slots"] == []
    assert normalized[1]["student_answer"] == ""
    assert normalized[1]["answer_state"] == "blank"
    assert json.dumps(raw, ensure_ascii=False) == before


def test_pure_drawing_rejects_invented_text_slot():
    row = {"question_number": "5", "student_slots": [{"slot_id": "5[1]", "student_answer": "猜测", "answer_state": "visible"}]}
    with pytest.raises(ValueError):
        bind_student_slots(row, "5", drawing())


def test_uncontracted_empty_list_still_cannot_bypass_text_evidence_state():
    with pytest.raises(ValueError):
        validate_visual_transcriptions([{"question_number": "5", "student_slots": []}])


def test_legacy_drawing_scalar_is_unchanged():
    row = {"question_number": "5", "student_answer": "图形已作答", "answer_state": "visible"}
    assert bind_student_slots(row, "5", drawing()) == row
