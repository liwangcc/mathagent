"""Physical slot labels: synthetic pixels; no OCR or business-data access."""
import copy
import numpy as np
import pytest
from PIL import Image, ImageDraw
from services import template_evidence as evidence
from services.template_anchor_crops import build_anchor_crop_plan
from services import template_evidence_sheets
from services import template_evidence_columns


def contract(kind='solve', mode='blank', columns=1):
    boxes = [[x, 100 + row * 200, x + 160, 180 + row * 200]
             for row in range(4) for x in (250, 710)]
    ids = ['2[3]', '2[1]', '2[5]', '2[7]', '2[8]', '2[9]', '2[11]', '2[12]']
    regions = [{'question_number': '2', 'regions': boxes, 'slot_ids': ids,
                'region_kinds': ['answer'] * 8}]
    schema = [{'question_number': '2', 'question_type': kind, 'slot_ids': ids,
               'region_kinds': ['answer'] * 8}]
    layout = {'page_type_id': 'page_1', 'template_mode': mode, 'question_numbers': ['2'],
              'question_schema': schema, 'template_answer_regions': regions,
              'template_handwriting_regions': copy.deepcopy(regions),
              'question_crop_plan': build_anchor_crop_plan({'column_layout': {'column_count': columns,
                'columns': [{'column_index': i + 1, 'x1_1000': round(i * 1000 / columns),
                            'x2_1000': round((i + 1) * 1000 / columns)} for i in range(columns)]}},
                artifact_sha256='a' * 64, expected_question_numbers=['2'])}
    return layout


def test_slot_metadata_survives_normalizers_and_legacy_stays_unchanged():
    layout = contract()
    assert evidence.normalize_template_handwriting_regions(layout['template_answer_regions']) == layout['template_answer_regions']
    schema = evidence.normalize_question_schema(layout['question_schema'])
    assert schema[0]['slot_ids'] == layout['question_schema'][0]['slot_ids']
    assert schema[0]['region_kinds'] == ['answer'] * 8
    old = [{'question_number': '2', 'regions': [[100, 100, 200, 200]]}]
    assert evidence.normalize_template_handwriting_regions(old) == old


@pytest.mark.parametrize('kind', ['fill', 'solve'])
@pytest.mark.parametrize('mode', ['blank', 'marked'])
@pytest.mark.parametrize('columns', [1, 2])
def test_each_region_uses_its_saved_id_across_columns(tmp_path, monkeypatch, kind, mode, columns):
    layout = contract(kind, mode, columns)
    original = copy.deepcopy(layout)
    pixels = np.full((800, 1200, 3), 254, np.uint8)  # exercise original-pixel patches
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    text = []
    actual_draw = ImageDraw.ImageDraw.text
    def capture(self, xy, value, *args, **kwargs):
        text.append(value)
        return actual_draw(self, xy, value, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', capture)
    rendered = evidence.build_full_column_difference_evidence_from_aligned(pixels,
        np.full(pixels.shape[:2], 255, np.uint8), np.full_like(pixels, 255), layout, tmp_path)
    placements = [p for c in rendered.diagnostics['evidence_columns'] for p in c['label_placements']]
    label_contract = rendered.diagnostics['qwen37_label_contract']
    assert label_contract['version'] == 'qwen37-blue-box-adaptive-slot-labels-v7'
    assert label_contract['slot_orientation_policy'] == 'horizontal-unless-preceding-region-collision'
    assert label_contract['source_pixels_recoverable'] is True
    assert all(p['orientation'] == 'horizontal' for p in placements)
    ids = layout['template_answer_regions'][0]['slot_ids']
    assert sorted(text) == sorted(line for p in placements for line in p.get('label_lines',[p['label_text']]))
    for p in placements:
        assert p['label_text']=='2.'+p['slot_id'][2:-1]  # same identity in every column
    assert {p['slot_id']: p['region_bbox_1000'] for p in placements} == dict(zip(ids, layout['template_answer_regions'][0]['regions']))
    assert layout == original
    from services.drawing_grading import reconstruct_canvas
    assert np.array_equal(reconstruct_canvas(rendered.evidence_paths, rendered.diagnostics), pixels)


@pytest.mark.parametrize('ids', [['2[1]'] * 8, ['3[1]'] * 8, ['2[1]'], None])
def test_invalid_explicit_slot_contract_not_silently_discarded(ids):
    regions = contract()['template_answer_regions']
    regions[0]['slot_ids'] = ids
    with pytest.raises(ValueError):
        evidence.normalize_template_handwriting_regions(regions)


def test_mixed_question_labels_only_text_slots_and_keeps_drawing_marker(tmp_path, monkeypatch):
    ids = ['2[1]', '2[2]', '2[3]']
    kinds = ['answer', 'drawing', 'answer']
    boxes = [[250, 100, 410, 180], [500, 300, 900, 600], [250, 720, 410, 800]]
    schema = [{'question_number': '2', 'question_type': 'solve', 'answer_kind': 'mixed',
               'drawing_regions': [boxes[1]], 'label_anchor': [490, 450],
               'slot_ids': ids, 'region_kinds': kinds}]
    pixels = np.full((800, 1200, 3), 254, np.uint8)
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    result = evidence.build_full_column_difference_evidence_from_aligned(pixels,
        np.full(pixels.shape[:2], 255, np.uint8), pixels,
        {'page_type_id': 'page_1', 'template_mode': 'blank', 'question_numbers': ['2'],
         'question_schema': schema, 'template_answer_regions': [{'question_number': '2', 'regions': boxes,
            'slot_ids': ids, 'region_kinds': kinds}]}, tmp_path)
    placements = result.diagnostics['evidence_columns'][0]['label_placements']
    assert {p['label_text'] for p in placements} == {'2.1', '2.3', 'Q2'}
    assert len([p for p in placements if p['source'] == 'drawing_region_left']) == 1
    assert result.diagnostics['blank_label_safety']['all_regions_labeled']


def test_slot_artifact_roundtrip_and_conflicting_reopen(tmp_path):
    layout = contract()
    image = tmp_path / 'source.png'
    Image.new('RGB', (1200, 800), 'white').save(image)
    artifact = evidence.build_template_artifact(str(image), str(tmp_path), 0,
        layout['question_schema'], template_mode='blank',
        template_answer_regions=layout['template_answer_regions'])
    import json
    frozen = json.loads(json.dumps({'page_1': artifact}))
    reopened = evidence.verify_template_artifacts(str(tmp_path), frozen)['page_1']
    assert reopened['question_schema'][0]['slot_ids'] == layout['question_schema'][0]['slot_ids']
    assert reopened['template_answer_regions'][0]['slot_ids'] == layout['question_schema'][0]['slot_ids']
    frozen['page_1']['template_answer_regions'][0]['slot_ids'].reverse()
    with pytest.raises(ValueError, match='slot|槽'):
        evidence.verify_template_artifacts(str(tmp_path), frozen)


def test_schema_and_region_binding_conflict_rejected(tmp_path, monkeypatch):
    layout = contract()
    layout['question_schema'][0]['slot_ids'] = list(reversed(layout['question_schema'][0]['slot_ids']))
    pixels = np.full((800, 1200, 3), 255, np.uint8)
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *a, **k: pixels.copy(),
    )
    with pytest.raises(ValueError, match='slot|槽'):
        evidence.build_full_column_difference_evidence_from_aligned(pixels,
            np.full(pixels.shape[:2], 255, np.uint8), pixels, layout, tmp_path)


@pytest.mark.parametrize('mode',['blank','marked'])
def test_full_evidence_blends_labels_but_drawing_replays_exact_source(tmp_path,monkeypatch,mode):
    from services.drawing_grading import reconstruct_canvas, build_text_evidence
    boxes=[[120,200,440,350],[445,200,750,350]]
    ids=['1[3]','1[8]']
    drawing={'question_number':'2','question_type':'solve','answer_kind':'drawing',
             'drawing_regions':[[600,650,900,900]],'label_anchor':[590,750]}
    layout={'page_type_id':'page_1','template_mode':mode,'question_numbers':['1','2'],
            'question_schema':[{'question_number':'1','question_type':'fill','slot_ids':ids},drawing],
            'template_answer_regions':[{'question_number':'1','regions':boxes,'slot_ids':ids}]}
    original=copy.deepcopy(layout)
    pixels=np.full((800,1200,3),255,np.uint8)
    pixels[200:225,510:530]=17  # last strokes in front box, under the second label
    pixels[305:309,80:970]=29   # overflowing every declared text answer box
    pixels[550:690,800:803]=20
    monkeypatch.setattr(
        template_evidence_sheets,
        "_answer_only_pixels_v2",
        lambda *a,**k:pixels.copy(),
    )
    monkeypatch.setattr(
        template_evidence_columns,
        "_answer_only_pixels_v2",
        lambda *a,**k:pixels.copy(),
    )
    result=evidence.build_full_column_difference_evidence_from_aligned(pixels,
        np.full(pixels.shape[:2],255,np.uint8),np.full_like(pixels,255),layout,tmp_path/'evidence')
    assert layout==original
    column=result.diagnostics['evidence_columns'][0]
    labels={p.get('slot_id'):p for p in column['label_placements'] if p.get('slot_id')}
    assert labels['1[3]']['label_opacity']==1
    assert labels['1[8]']['label_opacity']==.65
    assert all(p['label_style']=='blue-hollow-box-v2' for p in labels.values())
    assert result.diagnostics['qwen37_label_contract']['version']=='qwen37-blue-box-adaptive-slot-labels-v7'
    assert result.diagnostics['qwen37_label_contract']['foreground_preserved'] is False
    with Image.open(result.evidence_path) as image:
        source=np.asarray(image.crop(column['label_layout']['source_rect_px']))
        assert np.array_equal(source[305:309,80:970],pixels[305:309,80:970])
        assert not np.array_equal(source[200:225,510:530],pixels[200:225,510:530])
    restored=reconstruct_canvas(result.evidence_paths,result.diagnostics)
    ink=np.any(pixels!=255,axis=2)
    assert np.array_equal(restored[ink],pixels[ink])
    assert np.array_equal(restored[520:720,720:1080],pixels[520:720,720:1080])
    text_paths=build_text_evidence(result.evidence_paths,result.diagnostics,[drawing],str(tmp_path/'text'))
    visible_text=reconstruct_canvas(text_paths,result.diagnostics,restore_source=False)
    assert np.array_equal(visible_text[200:225,510:530],source[200:225,510:530])
    assert (visible_text[520:720,720:1080]==255).all()
    # Losing the raw-pixel payload must never feed the blended image to a
    # graphic pixel gate; the OCR image remains usable as an OCR-only image.
    with pytest.raises(ValueError,match='label source'):
        reconstruct_canvas(text_paths,result.diagnostics)
    assert np.array_equal(reconstruct_canvas(result.evidence_paths,result.diagnostics),restored)


def test_prefreeze_keeps_typed_question_rule_for_impossibly_dense_labels():
    from services.processing_errors import TemplateSchemaError, describe_failure
    boxes=[[400,200,410,240],[411,200,421,240]]
    ids=['1[1]','1[2]']
    with pytest.raises(TemplateSchemaError) as caught:
        evidence._render_column_question_labels(np.full((400,800,3),255,np.uint8),
            question_numbers=['1'],region_question_numbers=['1'],
            answer_regions=[{'question_number':'1','regions':boxes,'slot_ids':ids}],
            handwriting=[],anchor_by_number={},solve_numbers=set(),drawing_questions={},
            column_left=0,page_width=800,reuse_question_anchor_labels=True)
    public=describe_failure(caught.value,stage='template')
    assert public['code']=='TEMPLATE_SCHEMA_INVALID' and public['retryable'] is False
    assert '第 1 题' in public['message'] and '同一行几乎重叠' in public['message']
