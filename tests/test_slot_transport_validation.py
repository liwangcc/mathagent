"""Validate the actual transport boundary, not only the cache's post-parse path."""
import json
import pytest
from services.ocr_service import _multi_pool_response_validator,validate_visual_transcriptions
from services.template_slots import slot_transcription_prompt

def rows():return [{'slot_id':f'2[{i}]','student_answer':str(i) if i!=2 else '', 'answer_state':'visible' if i!=2 else 'blank'} for i in range(1,9)]
def prompt():return '[task=student_transcription_only]\nquestion_numbers=["2"]\nanswer_state student_answer'+slot_transcription_prompt([{'question_no':'2','slot_ids':[f'2[{i}]' for i in range(1,9)]}])

def test_transport_accepts_slot_only_answer_with_empty_legacy_scalar():
 raw={'questions':[{'question_number':'2','student_answer':'','answer_state':'visible','student_slots':rows()}]}
 _multi_pool_response_validator(prompt())(json.dumps(raw))
 # Without the slot contract the empty scalar contradicts 'visible': that
 # question fails closed on its own instead of rejecting the page.
 scalar=validate_visual_transcriptions(raw['questions'])[0]
 assert scalar['answer_state']=='unreadable' and scalar['student_answer']=='[无法识别]'

@pytest.mark.parametrize('mutation',['missing','duplicate','foreign','not-list'])
def test_transport_never_uses_scalar_to_override_invalid_slots(mutation):
 slots=rows()
 if mutation=='missing':slots.pop()
 if mutation=='duplicate':slots[1]['slot_id']='2[1]'
 if mutation=='foreign':slots[1]['slot_id']='9[2]'
 if mutation=='not-list':slots='1;2;3'
 got=validate_visual_transcriptions([{'question_number':'2','student_answer':'perfect scalar','answer_state':'visible','student_slots':slots}],slot_contracts={'2':[f'2[{i}]' for i in range(1,9)]})
 assert got[0]['answer_state']=='unreadable'
 assert got[0]['slot_identity_error']
 assert all(s['answer_state']=='unreadable' for s in got[0]['student_slots'])
