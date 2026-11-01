import pytest
from services.scoring_engine import build_grading_units
from tests.test_slot_scoring_identity import contract

@pytest.mark.parametrize('category',['daily','exam'])
def test_pure_drawing_without_text_slots_keeps_whole_pixel_judgment(category):
 amap=contract('solve',['7[2]','7[5]'])
 amap['7'].update(answer_kind='drawing',region_kinds=['drawing','drawing'],answer_slots=['',''],subquestion_count=1,subquestion_scores=[])
 row={'question_no':'7','student_answer':'图形已作答','answer_state':'visible'}
 units=build_grading_units([row],amap,category)
 assert len(units)==1 and units[0]['key']=='7'
 assert units[0]['student_answer']=='图形已作答' and units[0]['answer_state']=='visible'
 assert units[0]['student_slots']==[]
 assert 'slot_identity_error' not in units[0]
 assert units[0]['entry']['answer_kind']=='drawing'
