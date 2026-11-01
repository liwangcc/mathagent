import json
import pytest
from services import template_slots as slots
from services.paper_cache import RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT


def test_contract_replaces_old_scalar_example_instead_of_appending_a_second_schema():
    questions=[{'question_no':'1','slot_ids':['1[1]']}, {'question_no':'2','slot_ids':['2[1]','2[2]']}]
    prompt=slots.build_slot_evidence_prompt(RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT, questions)
    assert '按作答框顺序用“；”分隔' not in prompt
    assert prompt.count('三、该返回什么') == 1
    schema=json.JSONDecoder().raw_decode(prompt.split('三、该返回什么')[1].split('JSON：')[1].lstrip())[0]
    row=schema['questions'][0]
    assert 'student_answer' not in row
    assert row['student_slots'][0]['slot_id']=='1[1]'
    assert '单槽和多槽题格式相同' in prompt
    assert '"2":["2[1]","2[2]"]' in prompt


def test_legacy_prompt_untouched_without_slot_contract():
    assert slots.build_slot_evidence_prompt('legacy', [{'question_no':'1'}])=='legacy'


def test_missing_schema_section_is_not_silently_appended():
    with pytest.raises(ValueError):slots.build_slot_evidence_prompt('unexpected schema',[{'question_no':'1','slot_ids':['1[1]']}])


def test_two_orientations_have_explicit_frozen_id_mapping_and_full_column_instructions():
    prompt=slots.slot_transcription_prompt([{'question_no':'1.2','slot_ids':['1.2[8]','1.2[3]']}])
    labels=json.loads(prompt.split('槽位标签对应表=',1)[1].splitlines()[0])
    assert labels==[{'slot_id':'1.2[8]','label_text':'1.2.8','label_lines':['1','.2','.8']},
                    {'slot_id':'1.2[3]','label_text':'1.2.3','label_lines':['1','.2','.3']}]
    assert '65%不透明度' in prompt
    # Supersedes the September no-border / always-stacked policy.
    assert '透明底、蓝字、蓝色空心小矩形框' in prompt
    assert '默认横排' in prompt and '只有横排标签会侵入左邻作答框时才竖排' in prompt
    assert '不绘制作答区大框' in prompt
    assert '无边框' not in prompt and '其他槽位可仍显示Q槽ID' not in prompt
    assert '从该题号右侧沿水平方向寻找同一行作答' in prompt
    assert '不把左邻答案挪到右侧空槽' in prompt
    assert '保持同一串笔迹的数字、正负号、小数点和分数完整' in prompt
    assert '少量越界和下方续行仍完整保留' in prompt
    assert '框外延伸笔迹也要读' in prompt
    assert '不是分数或学生作答' in prompt
    assert '返回原始slot_id' in prompt
    assert '不解题' in slots.build_slot_evidence_prompt(RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT,
                                                      [{'question_no':'1','slot_ids':['1[1]']}])
