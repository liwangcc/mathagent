"""Choice physical slots cannot contradict the teacher-confirmed whole answer."""
import pytest
from services.grading_contract import finalize_answer_spec_for_category


def choice(**kw):
    return {"question_no":"2", "question_type":"choice", "choice_mode":"single",
            "standard_answer":"C", "blank_count":1, "answer_slots":["C"],
            "slot_ids":["2[1]"], "region_kinds":["answer"], **kw}


@pytest.mark.parametrize('category',['daily','exam'])
def test_choice_slot_and_standard_must_agree(category):
    with pytest.raises(ValueError, match="选择题.*答案槽"):
        finalize_answer_spec_for_category(choice(answer_slots=['D']),category)


def test_choice_group_cannot_be_disguised_as_single_choice():
    with pytest.raises(ValueError, match="独立选择题"):
        finalize_answer_spec_for_category(choice(blank_count=2,answer_slots=['C','C'],slot_ids=['2[1]','2[2]'],region_kinds=['answer','answer']),'daily')


def test_equivalent_choice_notation_remains_valid():
    result=finalize_answer_spec_for_category(choice(standard_answer='选 c',answer_slots=['C']),'daily')
    assert result.standard_answer=='C' and result.answer_slots==['C']


def test_legacy_choice_without_slot_identity_is_unchanged():
    result=finalize_answer_spec_for_category({'question_no':'2','question_type':'choice','choice_mode':'single','standard_answer':'C'},'daily')
    assert result.standard_answer=='C' and result.slot_ids is None
