"""Production cache with synthetic OCR boundary; no network or business DB."""
import asyncio
import copy
import json
import pytest
from services.paper_cache import PaperCache
from services.template_slots import make_slot_ids, sync_artifact_slots


def contract():
    return {'question_no': '2', 'question_text': '计算', 'question_type': 'solve',
            'standard_answer': '101；102；103；104；105；106；107；108',
            'answer_slots': [str(x) for x in range(101, 109)], 'blank_count': 8,
            'slot_ids': make_slot_ids('2', 8), 'region_kinds': ['answer'] * 8}


def response():
    return {'question_number': '2', 'student_slots': [
        {'slot_id': f'2[{i}]', 'student_answer': '' if i == 2 else str(100+i),
         'answer_state': 'blank' if i == 2 else 'visible'} for i in reversed(range(1, 9))]}


def run_cache(monkeypatch, raw, **kwargs):
    cache = PaperCache.from_confirmed_template_pages([{'page_index': 0, 'questions': [contract()]}])
    captured = {}
    async def vision(paths, prompt, **request):
        captured.update(prompt=prompt, request=request)
        return json.dumps({'questions': [raw]})
    monkeypatch.setattr('services.ocr_service.call_vision_images', vision)
    result = asyncio.run(cache._recognize_answers_only('ignored.png', 'page_1', evidence_mode=True, evidence_paths=['synthetic.png'], evidence_strategy='full_column_difference', **kwargs))
    return result, captured


def test_cache_keeps_structured_order_and_empty_slot(monkeypatch):
    result, captured = run_cache(monkeypatch, response())
    assert 'error' not in result, result
    q = result['questions'][0]
    assert [s['slot_id'] for s in q['student_slots']] == make_slot_ids('2', 8)
    assert q['student_slots'][1]['student_answer'] == ''
    assert q['student_slots'][2]['student_answer'] == '103'
    assert 'student_slots' in captured['prompt'] and '2[8]' in captured['prompt']
    assert contract()['standard_answer'] not in captured['prompt']
    assert captured['request']['max_attempts'] == 1


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'foreign', 'scalar'])
def test_bad_slot_contract_is_unreadable_not_shifted(monkeypatch, mutation):
    raw = response()
    if mutation == 'missing': raw['student_slots'].pop()
    if mutation == 'duplicate': raw['student_slots'].append(copy.deepcopy(raw['student_slots'][0]))
    if mutation == 'foreign': raw['student_slots'][0]['slot_id'] = '3[8]'
    if mutation == 'scalar': raw = {'question_number':'2','student_answer':'101；103','answer_state':'visible'}
    result, _ = run_cache(monkeypatch, raw)
    assert 'error' not in result, result
    q = result['questions'][0]
    assert q['slot_identity_error']
    assert all(s['answer_state'] == 'unreadable' for s in q['student_slots'])


def test_source_failure_invalidates_structured_slots(monkeypatch):
    result, _ = run_cache(monkeypatch, response(), program_unreadable_qns={'2'})
    assert all(s['answer_state'] == 'unreadable' for s in result['questions'][0]['student_slots'])


def test_sync_keeps_legacy_unchanged_and_materializes_reviewed_order():
    q = contract(); q.pop('slot_ids')
    boxes = [[50, 100+i*50, 200, 140+i*50] for i in range(8)]
    artifact = {'question_schema': [{'question_number':'2','question_type':'solve'}], 'template_answer_regions':[{'question_number':'2','regions':boxes}]}
    assert sync_artifact_slots(artifact, [q]) == artifact
    saved = sync_artifact_slots(artifact, [q], materialize=True)
    assert saved['question_schema'][0]['slot_ids'] == make_slot_ids('2', 8)
    assert saved['template_answer_regions'][0]['regions'] == boxes
    assert q['slot_ids'] == make_slot_ids('2', 8)


def test_sync_rebinds_reordered_ids_and_rejects_conflicting_ids():
    # The slot ID is the box identity: a reordered list rebinds to contract
    # order; only a different set of IDs is a conflict.
    q = contract()
    boxes = [[i * 10, 0, i * 10 + 5, 10] for i in range(8)]
    artifact = {'template_answer_regions':[{'question_number':'2','regions':list(reversed(boxes)),'slot_ids':list(reversed(q['slot_ids']))}]}
    region = sync_artifact_slots(artifact, [q])['template_answer_regions'][0]
    assert region['slot_ids'] == q['slot_ids'] and region['regions'] == boxes
    conflicting = {'template_answer_regions':[{'question_number':'2','regions':boxes,'slot_ids':[*q['slot_ids'][:7], '2[9]']}]}
    with pytest.raises(ValueError, match='槽编号与题目不一致'):
        sync_artifact_slots(conflicting, [contract()])


def test_pure_drawing_has_no_text_slot_obligation():
    from services.template_slots import bind_student_slots, slot_transcription_prompt
    contract = {'slot_ids': ['2[1]'], 'region_kinds': ['drawing'], 'question_no': '2'}
    observed = {'question_number': '2', 'answer_state': 'visible', 'student_answer': '图形已作答'}
    assert bind_student_slots(observed, '2', contract) == observed
    assert slot_transcription_prompt([contract]) == ''
