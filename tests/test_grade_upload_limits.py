import asyncio
import threading
import time
import pytest
from fastapi import HTTPException

from routers import grade_template
from routers import grade_uploads


class _Upload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        if size < 0:
            chunk = self._content[self._offset:]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def _safe_validation(filename: str, content: bytes) -> dict:
    return {"safe": True, "reason": "", "sanitized_filename": filename}


def test_batch_grade_upload_count_limit_rejects_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(grade_uploads, "MAX_UPLOAD_FILES", 2)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(grade_uploads._save_batch_uploads([
            _Upload("one.png", b"1"),
            _Upload("two.png", b"2"),
            _Upload("three.png", b"3"),
        ], str(tmp_path)))

    assert exc_info.value.status_code == 413
    assert "最多" in str(exc_info.value.detail)
    assert list(tmp_path.iterdir()) == []


def test_batch_grade_upload_total_limit_cleans_partial_files(tmp_path, monkeypatch):
    monkeypatch.setattr(grade_uploads, "MAX_UPLOAD_FILE_SIZE", 10)
    monkeypatch.setattr(grade_uploads, "MAX_UPLOAD_TOTAL_SIZE", 12)
    monkeypatch.setattr(grade_uploads, "validate_upload_path", _safe_validation)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(grade_uploads._save_batch_uploads([
            _Upload("first.png", b"12345678"),
            _Upload("second.png", b"abcdefgh"),
        ], str(tmp_path)))

    assert exc_info.value.status_code == 413
    assert "总大小" in str(exc_info.value.detail)
    assert list(tmp_path.iterdir()) == []


def test_batch_grade_rejects_pdf_before_ocr_and_cleans_batch(tmp_path):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(grade_uploads._save_batch_uploads([
            _Upload("papers.pdf", b"%PDF-1.7"),
        ], str(tmp_path)))

    assert exc_info.value.status_code == 400
    assert "不支持 PDF" in str(exc_info.value.detail)
    assert list(tmp_path.iterdir()) == []


def test_batch_grade_rejects_low_storage_before_reading_upload(tmp_path, monkeypatch):
    upload = _Upload("paper.png", b"synthetic-image")

    async def storage_low(*_args, **_kwargs):
        return {
            "ready": False,
            "status": "unavailable",
            "code": "upload_storage_insufficient",
        }

    monkeypatch.setattr(
        grade_uploads,
        "get_upload_storage_readiness_async",
        storage_low,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(grade_uploads._save_batch_uploads([upload], str(tmp_path)))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "SERVER_STORAGE_UNAVAILABLE"
    assert upload._offset == 0
    assert list(tmp_path.iterdir()) == []


def test_batch_upload_validation_runs_off_the_event_loop(tmp_path, monkeypatch):
    event_loop_thread = threading.get_ident()
    validation_threads = []

    def validate(filename, content):
        validation_threads.append(threading.get_ident())
        return _safe_validation(filename, content)

    monkeypatch.setattr(grade_uploads, "validate_upload_path", validate)

    saved = asyncio.run(grade_uploads._save_batch_uploads([
        _Upload("paper.png", b"synthetic-image"),
    ], str(tmp_path)))

    assert len(saved) == 1
    assert validation_threads
    assert all(thread_id != event_loop_thread for thread_id in validation_threads)


def test_batch_upload_preserves_browser_ocr_ready_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(grade_uploads, "validate_upload_path", _safe_validation)

    saved = asyncio.run(grade_uploads._save_batch_uploads([
        _Upload("student.ocr-ready-v1.jpg", b"synthetic-image"),
    ], str(tmp_path)))

    assert len(saved) == 1
    assert saved[0][1].endswith(".ocr-ready-v1.jpg")


def test_template_cpu_work_uses_the_shared_bounded_gate(monkeypatch):
    async def exercise():
        gate = asyncio.Semaphore(1)
        monkeypatch.setattr(
            grade_template,
            "_get_local_ocr_preprocess_semaphore",
            lambda: gate,
        )
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        worker_threads = []
        event_loop_thread = threading.get_ident()

        def work(value):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                worker_threads.append(threading.get_ident())
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return value

        results = await asyncio.gather(*(
            grade_template._run_template_cpu_component(work, value)
            for value in range(3)
        ))
        return results, max_active, worker_threads, event_loop_thread

    results, max_active, worker_threads, event_loop_thread = asyncio.run(exercise())

    assert results == [0, 1, 2]
    assert max_active == 1
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)
