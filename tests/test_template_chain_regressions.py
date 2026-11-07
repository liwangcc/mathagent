"""Regression evidence for t_461c5990; no network or business data."""
import asyncio
import copy

import numpy as np
from PIL import Image
import pytest

from services import analyzer, template_directory_pipeline as pipeline, template_evidence
from services.teaching_context import make_teaching_context, use_teaching_context
from services.template_recognition import _render_coordinate_grid
from tests import legacy_template_hierarchy as legacy
from services import ocr_service_template_route


def test_locator_column_grid_retains_full_page_coordinates(tmp_path, monkeypatch):
    image = Image.new('RGB', (803, 1000), 'white')
    path = tmp_path / 'paper.png'
    image.save(path)
    observed = []
    async def request(images, prompt, phase, validator, **kwargs):
        if phase == 'template-hierarchy-v1':
            return legacy._validate_flash_question_groups({'questions': [{'question_number': '1', 'answer_region_count': 1}]})
        if phase == 'template-directory-v1':
            for p in images:
                with Image.open(p) as source:
                    observed.append(np.asarray(source.convert('RGB')))
        raise RuntimeError('stop after observing locator input')
    monkeypatch.setattr(pipeline, '_request', request)
    columns = [{'column_index': 1, 'x1_1000': 0, 'x2_1000': 400},
               {'column_index': 2, 'x1_1000': 400, 'x2_1000': 1000}]
    with use_teaching_context(make_teaching_context('junior')):
        asyncio.run(pipeline.recognize_directory_template(str(path), template_mode='marked',
                    layout_result={'column_layout': {'columns': columns}}))
    expected = np.asarray(_render_coordinate_grid(image))
    boundary = round(400 * 803 / 1000)
    assert len(observed) == 2
    assert np.array_equal(observed[0], expected[:, :boundary])
    assert np.array_equal(observed[1], expected[:, boundary:])


@pytest.mark.parametrize('bounds', [[(-10, 1000)], [(0, 450), (500, 1000)], [(0, 600), (500, 1000)]])
def test_bad_column_geometry_stops_before_paid_requests(tmp_path, monkeypatch, bounds):
    path = tmp_path / 'paper.png'
    Image.new('RGB', (400, 600), 'white').save(path)
    calls = []
    async def request(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('must not call models')
    monkeypatch.setattr(pipeline, '_request', request)
    with use_teaching_context(make_teaching_context('junior')):
        result = asyncio.run(pipeline.recognize_directory_template(str(path), template_mode='blank',
            layout_result={'column_layout': {'columns': [
                {'column_index': i, 'x1_1000': a, 'x2_1000': b} for i, (a, b) in enumerate(bounds, 1)]}}))
    assert result.get('error')
    assert not calls


@pytest.mark.parametrize('mode', ['blank', 'marked'])
@pytest.mark.parametrize('has_student_ink', [False, True])
def test_approved_exemption_preserves_all_valid_student_pixels(mode, has_student_ink):
    # Includes a faint pencil value rejected by the former student_dark gate.
    student = np.full((80, 120, 3), 255, np.uint8)
    if has_student_ink:
        student[30:45, 45:70] = 245
        student[36:40, 47:66] = 30
    template = student.copy() if mode == 'marked' else np.full_like(student, 255)
    mask = np.full(student.shape[:2], 255, np.uint8)
    mask[34, 50] = 0
    boxes = [{'question_number': '1', 'regions': [[300, 300, 650, 650]]}]
    actual = template_evidence._answer_only_pixels_v2(student, template, mask,
                                                    template_handwriting_regions=boxes)
    approved = template_evidence._template_handwriting_region_mask(boxes, 120, 80)
    valid = approved & (mask == 255)
    assert np.array_equal(actual[valid], student[valid])
    assert np.all(actual[mask == 0] == 255)


@pytest.mark.parametrize('mode', ['blank', 'marked'])
def test_refresh_uses_answer_contract_even_without_handwriting_field(tmp_path, monkeypatch, mode):
    import routers.grade as grade
    from routers import grade_template, grade_template_artifacts
    artifact = {'question_numbers': ['1'], 'template_answer_regions': [
        {'question_number': '1', 'regions': [[300, 300, 650, 650]]}]}
    monkeypatch.setattr(grade_template_artifacts, 'materialize_template_difference_artifact', lambda directory, value: value)
    original = copy.deepcopy(artifact)
    result = grade_template._refresh_template_difference_artifact(str(tmp_path), artifact, template_mode=mode)
    assert result['template_handwriting_regions'] == artifact['template_answer_regions']
    assert artifact == original


@pytest.mark.parametrize('anchor,drawing', [(None, [[100, 100, 400, 300]]), ([150, 200], [[100, 100, 400, 300]]), ([50, 200], [])])
def test_incomplete_drawing_geometry_is_review_not_silent_ready(anchor, drawing):
    hierarchy = legacy._validate_flash_question_groups({'questions': [{'question_number': '1', 'answer_region_count': 1}]})
    payload = {'coordinate_space': 'normalized_1000_v1', 'questions': [{
        'question_number': '1', 'question_number_bbox': [20, 50, 40, 70],
        'question_type': 'fill', 'choice_mode': 'unknown', 'requires_drawing': True,
        'answer_kind': 'mixed', 'regions': [[100, 100, 400, 300]], 'drawing_regions': drawing,
        'label_anchor': anchor, 'slot_ids': ['1[1]']}], 'needs_review': []}
    assert pipeline._validate_locator(payload, hierarchy)['needs_review']


@pytest.mark.parametrize('phase,stage,model', [
    ('template-hierarchy-v1', 'template', 'qwen3.8-flash'),
    ('template-directory-v1', 'template', 'qwen3.8-max'),
    ('template-max-fullsolve-v1', 'solve', 'qwen3.8-max'),
    ('template-flash-transcribe-v1', 'transcription', 'qwen3.8-flash'),
])
def test_template_transport_error_keeps_stage_and_identity(monkeypatch, phase, stage, model):
    from services import flash_grading, ocr_service
    from services.processing_errors import report_failure
    failure = ConnectionError('private upstream details')
    async def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(pipeline, 'call_gpt_vision', fail)
    monkeypatch.setattr(flash_grading, 'call_flash', fail)
    monkeypatch.setattr(
        ocr_service_template_route,
        "_encode_vision_image",
        lambda path: ('aW1hZ2U=', 'image/png'),
    )
    monkeypatch.setattr(ocr_service, "_encode_vision_image", lambda path: ('aW1hZ2U=', 'image/png'))
    with pytest.raises(ConnectionError) as caught:
        asyncio.run(pipeline._request(['unused.png'], 'prompt', phase, lambda value: value))
    assert caught.value is failure
    info = report_failure(failure, stage='template')
    assert info['stage'] == stage
    assert failure._processing_diagnostic['metadata']['model'] == model
    assert failure._processing_diagnostic['metadata']['phase'] == phase
    assert report_failure(failure, stage='request')['error_id'] == info['error_id']
    assert 'private upstream details' not in str(info)


def test_hierarchy_failure_returns_structured_error(tmp_path, monkeypatch):
    from services import flash_grading
    image = tmp_path / 'paper.png'
    Image.new('RGB', (100, 200), 'white').save(image)
    async def fail(*args, **kwargs):
        raise ConnectionError('private upstream details')
    monkeypatch.setattr(flash_grading, 'call_flash', fail)
    with use_teaching_context(make_teaching_context('junior')):
        result = asyncio.run(pipeline.recognize_directory_template(
            str(image), template_mode='blank', layout_result={}))
    assert result['questions'] == []
    assert result['error_info']['stage'] == 'template'
    assert result['error_info']['error_id']


@pytest.mark.parametrize('explicit', [False, True])
def test_student_transcription_cannot_silently_enable_thinking(monkeypatch, explicit):
    from services import ocr_service
    monkeypatch.setattr(
        ocr_service_template_route,
        "_encode_vision_image",
        lambda path: ('aW1hZ2U=', 'image/png'),
    )
    monkeypatch.setattr(ocr_service, "_encode_vision_image", lambda path: ('aW1hZ2U=', 'image/png'))
    monkeypatch.setenv('OCR_TRANSCRIPTION_REASONING_EFFORT', 'high' if not explicit else 'none')
    async def forbidden(*args, **kwargs):
        raise AssertionError('must reject thinking before provider admission')
    monkeypatch.setattr(ocr_service, '_post_vision_request', forbidden)
    with pytest.raises(ValueError, match='transcription requires thinking disabled'):
        asyncio.run(ocr_service._call_vision_request(
            'unused.png', 'prompt', reasoning_effort='high' if explicit else None))
