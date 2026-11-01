import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser

from app import (
    GradingMultipartGuardMiddleware,
    _MULTIPART_MEMORY_SPOOL_BYTES,
    _check_grading_dependency,
)


_GRADING_ENTRY_PATHS = [
    "/api/batch-grade/template/recognize",
    "/api/batch-grade/recognize",
    "/api/answer-sheet/sessions/demo/source/upload",
    "/api/answer-sheet/sessions/demo/layout/upload",
    "/api/answer-sheet/sessions/demo/students/upload",
]


def _mock_infrastructure_dependencies(monkeypatch, model_state=None):
    """Keep global readiness and its Flash dispatcher real, without I/O."""
    import config

    ready = {"configured": True, "ready": True}
    monkeypatch.setattr(config, "ANSWER_SHEET_GRADING_ENABLED", True)
    storage = AsyncMock(return_value={"ready": True, "status": "available", "code": "ready"})
    ocr = AsyncMock(return_value=ready)
    flash = Mock(return_value=model_state if model_state is not None else ready)
    monkeypatch.setattr("services.system_readiness.get_upload_storage_readiness_async", storage)
    monkeypatch.setattr("services.model_concurrency.get_model_limiter", lambda: object())
    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", ocr)
    monkeypatch.setattr("services.flash_grading.get_flash_readiness", flash)
    return storage, ocr, flash


def _exercise_real_grading_guard(path, *, downstream_status=204):
    calls = {"receive": 0, "downstream": 0}
    messages = []

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"synthetic", "more_body": False}

    async def downstream(_scope, receive_body, send):
        calls["downstream"] += 1
        if downstream_status != 401:
            await receive_body()
        await send({"type": "http.response.start", "status": downstream_status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": [
        (b"content-type", b"multipart/form-data; boundary=synthetic"),
        (b"content-length", b"9"),
    ]}
    asyncio.run(GradingMultipartGuardMiddleware(downstream)(scope, receive, send))
    return calls, messages


@pytest.mark.parametrize("path", _GRADING_ENTRY_PATHS)
def test_real_grading_readiness_allows_configured_provider(monkeypatch, path):
    storage, ocr, flash = _mock_infrastructure_dependencies(monkeypatch)
    calls, messages = _exercise_real_grading_guard(path)
    assert messages[0]["status"] == 204
    assert calls == {"receive": 1, "downstream": 1}
    storage.assert_awaited_once()
    assert storage.await_args.kwargs == {"required_copies": 2}
    ocr.assert_awaited_once()
    assert flash.call_count == 1


@pytest.mark.parametrize("path", _GRADING_ENTRY_PATHS)
@pytest.mark.parametrize("model_state", [
    {"configured": False, "ready": False},
    {"configured": True, "ready": False},
])
def test_real_flash_unavailability_blocks_before_body(monkeypatch, path, model_state):
    _storage, ocr, flash = _mock_infrastructure_dependencies(monkeypatch, model_state)
    calls, messages = _exercise_real_grading_guard(path)
    assert calls == {"receive": 0, "downstream": 0}
    assert messages[0]["status"] == 503
    detail = json.loads(messages[1]["body"])["detail"]
    assert detail == {"error": "GRADING_CHAIN_UNAVAILABLE", "status": "unavailable",
                      "message": "批改服务当前不可用，请等待恢复后再上传。"}
    ocr.assert_awaited_once()
    flash.assert_called_once()


def test_ready_flash_does_not_replace_downstream_authorization(monkeypatch):
    _mock_infrastructure_dependencies(monkeypatch)
    calls, messages = _exercise_real_grading_guard(_GRADING_ENTRY_PATHS[1], downstream_status=401)
    assert messages[0]["status"] == 401
    assert calls == {"receive": 0, "downstream": 1}


@pytest.mark.parametrize("path", _GRADING_ENTRY_PATHS[2:])
def test_ready_flash_does_not_enable_disabled_answer_sheet(monkeypatch, path):
    import config

    storage, ocr, flash = _mock_infrastructure_dependencies(monkeypatch)
    monkeypatch.setattr(config, "ANSWER_SHEET_GRADING_ENABLED", False)
    calls, messages = _exercise_real_grading_guard(path)
    assert calls == {"receive": 0, "downstream": 0}
    assert messages[0]["status"] == 503
    assert json.loads(messages[1]["body"])["detail"]["status"] == "unavailable"
    storage.assert_not_awaited()
    ocr.assert_not_awaited()
    flash.assert_not_called()


def test_real_flash_probe_failure_stays_fault_before_body(monkeypatch):
    _storage, _ocr, flash = _mock_infrastructure_dependencies(monkeypatch)
    flash.side_effect = ConnectionError("synthetic private coordinator detail")
    calls, messages = _exercise_real_grading_guard(_GRADING_ENTRY_PATHS[1])
    assert calls == {"receive": 0, "downstream": 0}
    assert messages[0]["status"] == 503
    detail = json.loads(messages[1]["body"])["detail"]
    assert detail == {"error": "GRADING_CHAIN_UNAVAILABLE", "status": "fault",
                      "message": "批改链路发生故障，请稍后重试或联系管理员。"}
    assert b"synthetic private" not in messages[1]["body"]
    flash.assert_called_once()


def test_multipart_files_roll_to_disk_after_64_kib(tmp_path, monkeypatch):
    boundary = b"mathagent-boundary"
    payload = b"x" * (_MULTIPART_MEMORY_SPOOL_BYTES + 1)
    body = b"".join([
        b"--" + boundary + b"\r\n",
        b'Content-Disposition: form-data; name="files"; filename="paper.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        payload,
        b"\r\n--" + boundary + b"--\r\n",
    ])

    async def parse_upload():
        async def stream():
            yield body

        parser = MultiPartParser(
            Headers({
                "content-type": (
                    f"multipart/form-data; boundary={boundary.decode('ascii')}"
                ),
            }),
            stream(),
        )
        form = await parser.parse()
        upload = form["files"]
        try:
            assert upload.file._max_size == 64 * 1024
            assert upload.file._rolled is True
            assert await upload.read() == payload
        finally:
            await upload.close()

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    asyncio.run(parse_upload())


def test_startup_dependency_failure_keeps_application_available(caplog):
    async def unavailable():
        raise ConnectionError("synthetic coordinator outage")

    ready = asyncio.run(_check_grading_dependency("test_coordinator", unavailable))

    assert ready is False
    assert "component=test_coordinator" in caplog.text
    assert "error=ConnectionError" in caplog.text
    assert "synthetic coordinator outage" not in caplog.text


def test_startup_dependency_accepts_sync_and_async_checks():
    async def async_ready():
        return None

    assert asyncio.run(_check_grading_dependency("sync", lambda: None)) is True
    assert asyncio.run(_check_grading_dependency("async", async_ready)) is True


def test_startup_dependency_timeout_keeps_event_loop_available(caplog):
    release = threading.Event()

    def blocked_sync_check():
        release.wait(timeout=1)

    async def exercise():
        event_loop_progressed = asyncio.Event()

        async def mark_progress():
            await asyncio.sleep(0)
            event_loop_progressed.set()

        marker = asyncio.create_task(mark_progress())
        try:
            ready = await _check_grading_dependency(
                "blocked_coordinator",
                blocked_sync_check,
                timeout_seconds=0.01,
            )
            return ready, event_loop_progressed.is_set()
        finally:
            release.set()
            await marker

    ready, event_loop_progressed = asyncio.run(exercise())

    assert ready is False
    assert event_loop_progressed is True
    assert "component=blocked_coordinator" in caplog.text
    assert "error=TimeoutError" in caplog.text


def test_grading_multipart_fault_is_rejected_before_body_receive(monkeypatch):
    calls = {"receive": 0, "downstream": 0}

    async def unavailable(_upload_dir, _request_bytes):
        return {"ready": False, "status": "fault", "code": "disk_stalled"}

    monkeypatch.setattr(
        "services.system_readiness.get_grading_infrastructure_readiness",
        unavailable,
    )

    async def downstream(_scope, receive, _send):
        calls["downstream"] += 1
        await receive()

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"payload", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/batch-grade/recognize",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"7"),
        ],
    }
    asyncio.run(GradingMultipartGuardMiddleware(downstream)(scope, receive, send))

    assert calls == {"receive": 0, "downstream": 0}
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 503


def test_grading_multipart_size_is_rejected_before_readiness_or_body(monkeypatch):
    calls = {"readiness": 0, "receive": 0}

    async def readiness(_upload_dir, _request_bytes):
        calls["readiness"] += 1
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_grading_infrastructure_readiness",
        readiness,
    )

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    async def downstream(_scope, _receive, _send):
        raise AssertionError("oversized body reached downstream application")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/answer-sheet/sessions/session-1/layout/upload",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", str(3 * 1024**3).encode("ascii")),
        ],
    }
    asyncio.run(GradingMultipartGuardMiddleware(downstream)(scope, receive, send))

    assert calls == {"readiness": 0, "receive": 0}
    assert messages[0]["status"] == 413


def test_ordinary_grading_admission_rejects_busy_request_before_body(monkeypatch):
    calls = {"readiness": 0, "downstream": 0, "first_receive": 0, "busy_receive": 0}

    async def readiness(_upload_dir, _request_bytes):
        calls["readiness"] += 1
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_grading_infrastructure_readiness",
        readiness,
    )

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/batch-grade/recognize",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"7"),
        ],
    }
    busy_scope = {**scope, "path": "/api/batch-grade/template/recognize"}

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def downstream(_scope, receive, send):
            calls["downstream"] += 1
            await receive()
            entered.set()
            await release.wait()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        async def first_receive():
            calls["first_receive"] += 1
            return {"type": "http.request", "body": b"payload", "more_body": False}

        async def busy_receive():
            calls["busy_receive"] += 1
            return {"type": "http.request", "body": b"payload", "more_body": False}

        middleware = GradingMultipartGuardMiddleware(downstream)
        first_messages = []
        busy_messages = []
        next_messages = []

        async def first_send(message):
            first_messages.append(message)

        async def busy_send(message):
            busy_messages.append(message)

        async def next_send(message):
            next_messages.append(message)

        first = asyncio.create_task(
            middleware(scope, first_receive, first_send)
        )
        await entered.wait()
        await asyncio.wait_for(
            middleware(busy_scope, busy_receive, busy_send),
            timeout=0.1,
        )
        release.set()
        await first
        await middleware(scope, first_receive, next_send)
        return first_messages, busy_messages, next_messages

    first_messages, busy_messages, next_messages = asyncio.run(exercise())

    assert calls == {
        "readiness": 2,
        "downstream": 2,
        "first_receive": 2,
        "busy_receive": 0,
    }
    assert first_messages[0]["status"] == 200
    assert busy_messages[0]["status"] == 503
    assert b"GRADING_UPLOAD_BUSY" in busy_messages[1]["body"]
    assert next_messages[0]["status"] == 200


def test_ordinary_grading_admission_releases_after_downstream_error(monkeypatch):
    async def readiness(_upload_dir, _request_bytes):
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_grading_infrastructure_readiness",
        readiness,
    )

    calls = 0

    async def downstream(_scope, receive, send):
        nonlocal calls
        calls += 1
        await receive()
        if calls == 1:
            raise RuntimeError("synthetic disconnect before response")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"payload", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/batch-grade/recognize",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"7"),
        ],
    }

    async def exercise():
        middleware = GradingMultipartGuardMiddleware(downstream)

        try:
            await middleware(scope, receive, lambda _message: None)
        except RuntimeError as exc:
            assert str(exc) == "synthetic disconnect before response"
        else:
            raise AssertionError("synthetic downstream failure was swallowed")

        messages = []

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    messages = asyncio.run(exercise())

    assert calls == 2
    assert messages[0]["status"] == 200


def test_non_grading_multipart_prepares_spool_before_body_receive(monkeypatch):
    calls = {"storage": 0, "receive": 0, "downstream": 0}

    async def storage_ready(_upload_dir, request_bytes, *, required_copies):
        calls["storage"] += 1
        assert request_bytes == 7
        assert required_copies == 2
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_upload_storage_readiness_async",
        storage_ready,
    )

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"payload", "more_body": False}

    async def downstream(_scope, receive_body, _send):
        calls["downstream"] += 1
        await receive_body()

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/vision-office",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"7"),
        ],
    }
    asyncio.run(GradingMultipartGuardMiddleware(downstream)(scope, receive, send))

    assert calls == {"storage": 1, "receive": 1, "downstream": 1}


def test_office_multipart_size_is_rejected_before_storage_or_body(monkeypatch):
    calls = {"storage": 0, "receive": 0}

    async def storage(*_args, **_kwargs):
        calls["storage"] += 1
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_upload_storage_readiness_async",
        storage,
    )

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    async def downstream(_scope, _receive, _send):
        raise AssertionError("oversized office body reached downstream application")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/vision-office",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", str(30 * 1024**2).encode("ascii")),
        ],
    }
    asyncio.run(
        GradingMultipartGuardMiddleware(downstream)(
            scope,
            receive,
            send,
        )
    )

    assert calls == {"storage": 0, "receive": 0}
    assert messages[0]["status"] == 413


def test_office_admission_rejects_waiter_before_body_receive(monkeypatch):
    calls = {"downstream": 0, "receive": 0}

    async def storage(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(
        "services.system_readiness.get_upload_storage_readiness_async",
        storage,
    )

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"payload", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/vision-office",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"7"),
        ],
    }

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def downstream(_scope, _receive, send):
            calls["downstream"] += 1
            if calls["downstream"] == 2:
                entered.set()
            await release.wait()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = GradingMultipartGuardMiddleware(
            downstream,
            admission_timeout_seconds=0.01,
        )
        first_messages = []
        second_messages = []
        third_messages = []

        async def first_send(message):
            first_messages.append(message)

        async def second_send(message):
            second_messages.append(message)

        async def third_send(message):
            third_messages.append(message)

        first = asyncio.create_task(
            middleware(scope, receive, first_send)
        )
        second = asyncio.create_task(
            middleware(scope, receive, second_send)
        )
        await entered.wait()
        await middleware(scope, receive, third_send)
        release.set()
        await asyncio.gather(first, second)
        return first_messages, second_messages, third_messages

    first_messages, second_messages, third_messages = asyncio.run(exercise())

    assert calls == {"downstream": 2, "receive": 0}
    assert first_messages[0]["status"] == 200
    assert second_messages[0]["status"] == 200
    assert third_messages[0]["status"] == 503
