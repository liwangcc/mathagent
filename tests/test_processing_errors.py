"""Fault injection at production boundaries; no real model or student data."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.processing_errors import (
    ProcessingError, classify_failure, current_correlation_id, describe_failure,
    failure_from_public, processing_context, processing_stage, record_attempt, report_failure,
)
from services.processing_http import install_processing_errors


@pytest.fixture
def events():
    records = []
    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))
    logger = logging.getLogger('mathagent.processing')
    handler = Capture()
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def status_error(status):
    request = httpx.Request('POST', 'https://invalid.test/?token=secret-canary')
    response = httpx.Response(status, request=request, text='student-canary')
    return httpx.HTTPStatusError('secret-canary student-canary', request=request, response=response)


@pytest.mark.parametrize('error,stage,expected', [
    (ValueError('bad file'), 'import', 'FORMAT_INVALID'),
    (ImportError('unrar'), 'import', 'DEPENDENCY_MISSING'),
    (httpx.ReadTimeout('secret-canary'), 'transcription', 'PROVIDER_TIMEOUT'),
    (status_error(429), 'transcription', 'PROVIDER_RATE_LIMITED'),
    (status_error(401), 'transcription', 'PROVIDER_AUTH_FAILED'),
    (RuntimeError('empty content'), 'transcription', 'TRANSCRIPTION_EMPTY'),
    (json.JSONDecodeError('bad json', 'student-canary', 0), 'solve', 'OUTPUT_PARSE_FAILED'),
    (ValueError('bbox outside image bounds'), 'image', 'IMAGE_COORDINATES_INVALID'),
    (status_error(503), 'model', 'PROVIDER_UNAVAILABLE'),
    (httpx.ConnectError('network'), 'model', 'NETWORK_ERROR'),
])
def test_fault_taxonomy(error, stage, expected, events):
    with processing_context() as cid:
        result = report_failure(error, stage=stage, model='qwen3.7-plus', route='dashscope',
                                attempt=2, retry_count=1, duration_ms=123, artifact_refs=['a' * 64, 'student-canary.png'],
                                api_key='secret-canary', student_name='student-canary')
    assert result['code'] == expected
    assert result['stage'] == stage
    assert result['correlation_id'] == cid
    assert result['action'] and result['message']
    assert len(events) == 1
    assert events[0]['model'] == 'qwen3.7-plus'
    assert events[0]['artifact_refs'] == ['a' * 64]
    wire = json.dumps(events, ensure_ascii=False)
    assert 'secret-canary' not in wire and 'student-canary' not in wire
    assert 'status_code' not in result


def test_causal_dedup_independent_and_retry_records(events):
    original = status_error(429)
    with processing_context() as cid:
        record_attempt(original, stage='transcription', attempt=1)
        first = report_failure(original, stage='transcription')
        try:
            raise RuntimeError('outer template failure') from original
        except RuntimeError as wrapper:
            assert report_failure(wrapper, stage='template') == first
        independent = report_failure(status_error(429), stage='transcription')
        retry = status_error(429)
        record_attempt(retry, stage='transcription', attempt=2)
        report_failure(retry, stage='transcription')
    assert independent['error_id'] != first['error_id']
    assert {x['correlation_id'] for x in events} == {cid}
    assert [x['event'] for x in events].count('processing.failed') == 3
    assert [x['event'] for x in events].count('processing.attempt_failed') == 2
    assert events[0]['status_code'] == 429


@pytest.mark.parametrize('code,expected', [
    ('TEMPLATE_CANONICALIZATION_FAILED', 'IMAGE_CANONICALIZATION_FAILED'),
    ('TEMPLATE_CANONICAL_CANDIDATE_INVALID', 'IMAGE_CANDIDATE_INVALID'),
    ('TEMPLATE_CROP_FAILED', 'IMAGE_COORDINATES_INVALID'),
])
def test_image_boundary_code_beats_generic_nested_value_error(code, expected):
    error = RuntimeError('safe boundary')
    error.code = code
    error.__cause__ = ValueError('synthetic validation canary')
    assert classify_failure(error, 'template') == expected


def test_specific_original_code_beats_outer_image_boundary():
    error = RuntimeError('safe boundary')
    error.code = 'TEMPLATE_CROP_FAILED'
    error.__cause__ = ProcessingError('PROVIDER_RATE_LIMITED')
    assert classify_failure(error, 'template') == 'PROVIDER_RATE_LIMITED'


def test_stage_preserves_exception_type_cause_and_source(events):
    with processing_context():
        try:
            with processing_stage('solve', model='deepseek-flash', route='subscription'):
                try:
                    raise TimeoutError('protected student-canary')
                except TimeoutError as cause:
                    raise RuntimeError('safe wrapper') from cause
        except RuntimeError as exc:
            assert isinstance(exc.__cause__, TimeoutError)
            info = report_failure(exc, stage='template')
    assert info['stage'] == 'solve'
    assert info['code'] == 'PROVIDER_TIMEOUT'
    assert events[0]['cause_chain'][-1]['type'] == 'TimeoutError'
    assert events[0]['cause_chain'][-1]['frames'][-1]['function'] == 'test_stage_preserves_exception_type_cause_and_source'
    assert events[0]['duration_ms'] >= 0
    assert 'student-canary' not in json.dumps(events)


def test_parallel_context_does_not_cross_requests(events):
    async def work(stage):
        with processing_context() as cid:
            with processing_stage(stage):
                await asyncio.sleep(0)
                observed = await asyncio.to_thread(current_correlation_id)
                info = report_failure(TimeoutError(), stage=stage)
                return cid, observed, info
    async def run():
        return await asyncio.gather(work('solve'), work('transcription'))
    results = asyncio.run(run())
    assert results[0][0] != results[1][0]
    assert all(cid == observed == info['correlation_id'] for cid, observed, info in results)
    assert current_correlation_id() == ''


def test_rehydrated_failure_does_not_report_again(events):
    info = report_failure(TimeoutError(), stage='template')
    copied = json.loads(json.dumps(info))
    assert report_failure(failure_from_public(copied)) == info
    assert len(events) == 1


def test_http_boundary_and_validation(events):
    app = FastAPI()
    install_processing_errors(app)
    @app.get('/api/batch-grade/template/fault')
    async def fault():
        with processing_stage('solve', model='deepseek-flash'):
            raise httpx.ReadTimeout('secret-canary student-canary')
    @app.get('/api/import/validate')
    async def validate(count: int):
        return count
    @app.get('/api/reject')
    async def reject():
        raise HTTPException(403, detail={'code': 'BUSINESS_FORBIDDEN', 'message': '无权操作'})
    with TestClient(app) as client:
        response = client.get('/api/batch-grade/template/fault', headers={'X-Request-ID': 'student-canary'})
        assert response.status_code == 500
        info = response.json()['error_info']
        assert info['stage'] == 'solve' and info['code'] == 'PROVIDER_TIMEOUT'
        assert response.headers['X-Request-ID'] == info['correlation_id']
        assert response.headers['X-Request-ID'] != 'student-canary'
        validation = client.get('/api/import/validate?count=student-canary')
        assert validation.status_code == 422
        assert validation.json()['error_info']['stage'] == 'import'
        assert 'student-canary' not in validation.text
        denied = client.get('/api/reject')
        assert denied.json()['detail']['code'] == 'BUSINESS_FORBIDDEN'
        assert denied.json()['error_info']['code'] == 'REQUEST_REJECTED'
    assert len(events) == 3
    assert 'secret-canary' not in json.dumps(events)


def test_ocr_adapter_is_safe_and_keeps_retry_policy(events):
    from services.ocr_service import ocr_failure_result
    error = status_error(429)
    result = ocr_failure_result(error, message='secret-canary student-canary')
    assert result['error_info']['code'] == 'PROVIDER_RATE_LIMITED'
    assert result['_ocr_retryable'] is True
    assert result['_ocr_error_code'] == 'provider_rate_limited'
    assert result['questions'] == []
    assert 'canary' not in json.dumps(result)
    report_failure(error, stage='template')
    assert len(events) == 1


def test_template_session_status_keeps_failure_identity(tmp_path, events):
    from services.template_session import TemplateSession
    from routers.grade_template import _template_recognition_status_payload
    session = TemplateSession('a' * 12, str(tmp_path))
    session.status = 'failed'
    session.error_info = report_failure(TimeoutError(), stage='template')
    session.error = session.error_info['message']
    session.save_meta()
    loaded = TemplateSession.load_meta(session.template_id, str(tmp_path))
    first = _template_recognition_status_payload(loaded)
    second = _template_recognition_status_payload(loaded)
    assert first['error_info'] == second['error_info'] == session.error_info
    assert not first['success'] and first['terminal']
    assert len(events) == 1


def test_template_relay_emits_one_error_with_persisted_id(tmp_path, events):
    from routers.grade_template import _relay_template_recognition, _TemplateRecognitionEventBuffer
    from services.template_session import TemplateSession
    async def run():
        session = TemplateSession('b' * 12, str(tmp_path))
        buffer = _TemplateRecognitionEventBuffer()
        async def source():
            raise TimeoutError('secret-canary')
            yield
        with processing_context():
            await _relay_template_recognition(source(), buffer, session)
        chunks = [chunk async for chunk in buffer.subscribe()]
        payload = json.loads(chunks[0].split('data: ', 1)[1])
        assert payload['error_info'] == session.error_info
        assert session.status == 'failed'
        assert payload['error_info']['code'] == 'PROVIDER_TIMEOUT'
        assert len(chunks) == 1
    asyncio.run(run())
    assert len(events) == 1


def test_real_jsonl_sink_once(tmp_path):
    from services.logging_config import setup_processing_logging
    logger = logging.getLogger('mathagent.processing')
    saved_handlers, saved_level, saved_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    try:
        setup_processing_logging(tmp_path)
        with processing_context() as cid:
            error = TimeoutError('secret-canary')
            report_failure(error, stage='solve', model='deepseek-flash')
            report_failure(error, stage='template')
        rows = [json.loads(line) for line in (tmp_path / 'processing.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(rows) == 1 and rows[0]['correlation_id'] == cid
        assert rows[0]['stage'] == 'solve'
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers, logger.level, logger.propagate = saved_handlers, saved_level, saved_propagate


def test_actual_student_ocr_retry_diagnostics(monkeypatch, tmp_path, events):
    from services import ocr_service as ocr
    image = tmp_path / 'probe.jpg'
    image.write_bytes(b'offline-image-probe')
    errors = []
    async def fail(*args, **kwargs):
        error = httpx.ReadTimeout('secret-canary')
        errors.append(error)
        raise error
    async def no_wait(_delay):
        pass
    monkeypatch.setattr(ocr, '_vision_pool', None)
    monkeypatch.setattr(ocr, '_post_vision_request', fail)
    monkeypatch.setattr(ocr.asyncio, 'sleep', no_wait)
    monkeypatch.setenv('DASHSCOPE_MODEL', 'qwen3.7-plus')
    with processing_context() as cid:
        with pytest.raises(httpx.ReadTimeout) as caught:
            asyncio.run(ocr._call_vision_request(str(image), 'offline', max_attempts=2, reasoning_effort='none'))
        result = ocr.ocr_failure_result(caught.value)
        report_failure(caught.value, stage='template')
    attempts = [x for x in events if x['event'] == 'processing.attempt_failed']
    terminal = [x for x in events if x['event'] == 'processing.failed']
    assert len(errors) == 2 and caught.value is errors[-1]
    assert [x['attempt'] for x in attempts] == [1, 2]
    assert [x['retry_count'] for x in attempts] == [0, 1]
    assert len(terminal) == 1
    assert {x['correlation_id'] for x in events} == {cid}
    assert result['error_info']['error_id'] == attempts[-1]['error_id']
    assert terminal[0]['model'] == 'qwen3.8-flash'
    assert 'secret-canary' not in json.dumps(events)


def test_flash_preserves_original_timeout_cause(monkeypatch, events):
    from services import flash_grading as flash, ocr_service as ocr
    from tests.test_flash_grading import Limiter
    original = httpx.ReadTimeout('secret-canary')
    async def fail(*args, **kwargs):
        raise original
    monkeypatch.setattr(ocr, '_vision_pool', None)
    monkeypatch.setattr(ocr, 'get_model_limiter', lambda: Limiter())
    monkeypatch.setattr(ocr, '_post_vision_request', fail)
    monkeypatch.setattr(flash, '_credential_failures', {})
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'offline-test-key')
    monkeypatch.setenv('DASHSCOPE_BASE_URL', 'https://unit.invalid/v1')
    with pytest.raises(flash.FlashGradingError) as caught:
        asyncio.run(flash.call_flash([{'role': 'user', 'content': '{}'}], phase='text'))
    assert caught.value.__cause__ is original
    info = report_failure(caught.value, stage='grading')
    assert info['code'] == 'PROVIDER_TIMEOUT'
    assert events[0]['model'] == flash.FLASH_MODEL
    assert events[0]['cause_chain'][-1]['type'] == 'ReadTimeout'
    assert 'secret-canary' not in json.dumps(events)


def test_generic_model_adapter_inherits_template_stage(events):
    from services.processing_errors import current_processing_stage, diagnosed
    @diagnosed('model')
    async def adapter():
        error = TimeoutError()
        describe_failure(error, stage=current_processing_stage('grading'))
        raise error
    async def run():
        with processing_stage('template', model='qwen3.8-flash', phase='template-hierarchy-v1'):
            await adapter()
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(run())
    info = report_failure(caught.value, stage='grading')
    assert info['stage'] == 'template'
    assert events[0]['phase'] == 'template-hierarchy-v1'


def test_template_pre_dispatch_deadline_keeps_solve_identity(events):
    from services import template_directory_pipeline as pipeline
    from services import template_one_three as one_three
    async def run():
        with pipeline.template_import_context(asyncio.get_running_loop().time() - 1, {'stages': {}}):
            await pipeline._request('unused.jpg', 'unused', one_three.SOLVE_PHASE, lambda x: x)
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(run())
    info = report_failure(caught.value, stage='template')
    assert info['stage'] == 'solve' and info['code'] == 'PROVIDER_TIMEOUT'
    assert events[0]['model'] == 'qwen3.8-max'
    assert events[0]['phase'] == one_three.SOLVE_PHASE


def test_template_post_transport_validator_keeps_phase(monkeypatch, events):
    from services import template_directory_pipeline as pipeline
    async def response(*args, **kwargs):
        return '{}'
    def invalid(_payload):
        raise ValueError('invalid response contract')
    monkeypatch.setattr(pipeline, 'call_gpt_vision', response)
    with pytest.raises(ValueError) as caught:
        asyncio.run(pipeline._request('unused.jpg', 'unused', 'template-directory-v1', invalid))
    info = report_failure(caught.value, stage='template')
    assert info['stage'] == 'template' and info['code'] == 'OUTPUT_PARSE_FAILED'
    assert events[0]['model'] == 'qwen3.8-max'
    assert events[0]['phase'] == 'template-directory-v1'


def test_rejected_requests_are_never_relabelled_from_chained_cause_text(events):
    """A 4xx is a rejected request. A chained pydantic "validation" or bare
    ValueError must not read as a model parse failure (incidents dfe229fa,
    2b5e7b50); a deliberately set code survives; 5xx keeps provider causes."""
    from pydantic import BaseModel
    from services.processing_errors import TemplateSchemaError

    class Strict(BaseModel):
        count: int

    app = FastAPI()
    install_processing_errors(app)

    @app.post('/api/batch-grade/template/confirm')
    def confirm():
        try:
            Strict.model_validate({'count': 'not-a-number'})
        except ValueError as exc:
            raise HTTPException(400, detail='第 1 页第 2 题未通过答案合同校验') from exc

    @app.put('/api/batch-grade/template/x/layout-overrides')
    def layout():
        try:
            raise TemplateSchemaError('slot_labels_overlap', '5')
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @app.post('/api/batch-grade/template/recognize')
    def provider():
        try:
            raise status_error(429)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(503, detail='busy') from exc

    with TestClient(app) as client:
        rejected = client.post('/api/batch-grade/template/confirm').json()
        schema = client.put('/api/batch-grade/template/x/layout-overrides').json()
        busy = client.post('/api/batch-grade/template/recognize').json()
    assert rejected['error_info']['code'] == 'FORMAT_INVALID'
    assert rejected['detail'] == '第 1 页第 2 题未通过答案合同校验'
    assert '模型返回结果不符合格式要求' not in rejected['error_info']['message']
    assert schema['error_info']['code'] == 'TEMPLATE_SCHEMA_INVALID'
    assert busy['error_info']['code'] == 'PROVIDER_RATE_LIMITED'
