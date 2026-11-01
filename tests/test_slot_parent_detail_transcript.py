"""Parent result must not display only the first physical slot."""
from services.scoring_engine import build_grading_units,aggregate_grading_result
from tests.test_slot_scoring_identity import contract,row

def test_parent_fill_detail_preserves_complete_transcript_and_blank_positions():
 units=build_grading_units([row()],contract(),'daily')
 result=aggregate_grading_result(units,{u['key']:{'result':'wrong' if u['answer_state']=='blank' else 'correct'} for u in units},'daily','test')
 parent=result['details'][0]
 assert parent['student_answer']=='A\nstep;still first；；C'
 assert parent['question_no']=='7' and 'slot_id' not in parent
 assert parent['slot_ids']==['7[1]','7[2]','7[3]']
 assert [b['student_answer'] for b in parent['blank_results']]==['A\nstep;still first','','C']

def test_parent_fill_detail_state_summarizes_all_slots_not_first_only():
 q=row();q['student_slots'][0]['answer_state']='unreadable';q['student_slots'][0]['student_answer']='?'
 units=build_grading_units([q],contract(),'daily')
 judgments={u['key']:{'result':'needs_review' if u['answer_state']=='unreadable' else 'correct'} for u in units}
 parent=aggregate_grading_result(units,judgments,'daily','test')['details'][0]
 assert parent['answer_state']=='unreadable'
 assert parent['result']=='needs_review'
 assert parent['student_answer'].endswith('；[无法识别]')
