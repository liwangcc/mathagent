"""Empty choice placeholder is a skip, not a global failure."""
import pytest
from services import template_one_three as contract
from services import template_directory_pipeline as pipeline
from tests.test_template_one_three import hierarchy_payload,branch_payloads
from tests import legacy_template_hierarchy as legacy

@pytest.mark.parametrize('choice',[True,False])
@pytest.mark.parametrize('uncertain',[[],['印刷内容不清楚']])
def test_empty_transcription_defers_to_locator_type(choice,uncertain):
 h=legacy._validate_flash_question_groups(hierarchy_payload((1,)))
 branches=branch_payloads(h,choice=choice)
 raw={'questions':[{'q_id':'Q1','question_text':'','uncertain':uncertain}]}
 received=contract.receive_transcription(raw,h)
 located=pipeline._validate_one_three_locator(branches['template-directory-v1'],h)
 solved=contract.receive_solver(branches[contract.SOLVE_PHASE],h)
 if not choice and not uncertain:
  merged=contract.merge_branches(located,solved,received)
  assert merged['answers'][0]['question_text']==''
  assert merged['answers'][0]['answer_status']=='needs_review'
  assert '正文为空' in ' '.join(merged['answers'][0]['answer_review_reasons'])
 else:
  merged=contract.merge_branches(located,solved,received)
  assert merged['answers'][0]['question_text']==''
  assert merged['answers'][0]['answer_status']==('ready' if choice else 'needs_review')


def test_empty_placeholder_does_not_hide_missing_nonchoice():
 h=legacy._validate_flash_question_groups(hierarchy_payload((1,)))
 b=branch_payloads(h)
 merged=contract.merge_branches(pipeline._validate_one_three_locator(b['template-directory-v1'],h),contract.receive_solver(b[contract.SOLVE_PHASE],h),{'questions':[]})
 assert merged['answers'][0]['question_text']==''
 assert merged['answers'][0]['answer_status']=='needs_review'
 assert 'Flash 省略了非选择题正文' in ' '.join(merged['answers'][0]['answer_review_reasons'])
