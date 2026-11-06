"""Readiness probes must not queue behind the OCR image work they measure.

Regression: a 23-paper batch saturated the default thread pool that
``get_ocr_readiness``'s probe shared with image preprocessing; the probe then
blew its 3s budget and the availability gate mislabeled "busy" as "fault",
which silently blocked ``POST /batch-grade/confirm`` (nginx: zero confirms).
"""

import asyncio
import time
from types import SimpleNamespace

import services.ocr_service as ocr_service
from routers import grade_availability


def _reset_probe_state(monkeypatch):
    monkeypatch.setattr(ocr_service, "_local_image_readiness_probe", None)
    monkeypatch.setattr(ocr_service, "_local_image_readiness_probe_loop", None)
    monkeypatch.setattr(ocr_service, "_local_image_readiness_cache", None)
    monkeypatch.setattr(ocr_service, "_local_image_readiness_cache_at", 0.0)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake-not-a-secret")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://fake.example/v1")


def _saturate_default_executor(loop, seconds=4.0, jobs=64):
    """Flood asyncio.to_thread's shared default pool like OCR image work does."""
    return [loop.run_in_executor(None, time.sleep, seconds) for _ in range(jobs)]


def _install_fast_chain(monkeypatch, grade):
    async def fake_storage(*args, **kwargs):
        return {"ready": True, "status": "ready"}

    async def fake_teacher(_teacher_id):
        return {"status": "available", "label": "可用"}

    def fake_grading_ready():
        return {"configured": True, "ready": True}

    async def fake_grading_ready_async(_limiter=None):
        return {"configured": True, "ready": True}

    class _LocalLimiter:
        pass

    monkeypatch.setattr(
        "services.system_readiness.get_upload_storage_readiness_async", fake_storage
    )
    monkeypatch.setattr(
        "services.ocr_teacher_fairness.public_teacher_availability", fake_teacher
    )
    monkeypatch.setattr("services.analyzer.get_grading_readiness", fake_grading_ready)
    monkeypatch.setattr(
        "services.analyzer.get_grading_readiness_async", fake_grading_ready_async
    )
    monkeypatch.setattr(
        "services.model_concurrency.get_model_limiter", lambda: _LocalLimiter()
    )


def test_readiness_probe_survives_default_pool_saturation(monkeypatch):
    """The probe lane is decoupled: image-work saturation cannot starve it."""
    async def scenario():
        _reset_probe_state(monkeypatch)
        loop = asyncio.get_running_loop()
        fillers = _saturate_default_executor(loop)
        try:
            started = time.monotonic()
            result = await ocr_service.get_ocr_readiness()
            elapsed = time.monotonic() - started
        finally:
            await asyncio.gather(*fillers, return_exceptions=True)
        assert result["ready"] is True
        assert result["reason"] == ""
        assert elapsed < 3.0

    asyncio.run(scenario())


def test_busy_probe_serves_last_successful_verdict(monkeypatch):
    """A wedged probe means busy: serve the last successful heartbeat, never a
    spurious not-ready flip (the 2026-09-22 silent-block regression)."""
    async def scenario():
        _reset_probe_state(monkeypatch)
        first = await ocr_service.get_ocr_readiness()
        assert first["ready"] is True

        def wedged_probe():
            time.sleep(4.5)
            return {"ready": True, "reason": ""}

        monkeypatch.setattr(ocr_service, "get_local_image_readiness", wedged_probe)
        monkeypatch.setattr(ocr_service, "_local_image_readiness_cache_at", 0.0)
        monkeypatch.setattr(ocr_service, "_local_image_readiness_probe", None)
        started = time.monotonic()
        second = await ocr_service.get_ocr_readiness()
        elapsed = time.monotonic() - started
        assert second == first
        assert elapsed >= 2.9

    asyncio.run(scenario())


def test_availability_budget_expiry_reports_queued_not_fault(monkeypatch):
    """Coordination budget expiry under load is "queued", not a chain fault."""
    import routers.grade as grade

    _install_fast_chain(monkeypatch, grade)

    async def fake_hung_ocr():
        await asyncio.sleep(30)
        return {"provider": "ocr", "configured": True, "ready": True, "reason": ""}

    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", fake_hung_ocr)
    monkeypatch.setattr(grade_availability, "READINESS_IO_TIMEOUT_SECONDS", 0.5)

    result = asyncio.run(
        grade._batch_grade_service_availability(SimpleNamespace(id=1))
    )
    assert result["status"] == "queued"
    assert result["label"] == "需排队"


def test_availability_inner_timeout_stays_fail_closed(monkeypatch):
    """An inner leg raising its own TimeoutError (e.g. Redis ping) is a real
    coordination fault — the busy/queued mapping must not swallow it."""
    import routers.grade as grade

    _install_fast_chain(monkeypatch, grade)

    async def fake_broken_ocr():
        raise TimeoutError("inner probe transport failure")

    monkeypatch.setattr("services.ocr_service.get_ocr_readiness", fake_broken_ocr)

    result = asyncio.run(
        grade._batch_grade_service_availability(SimpleNamespace(id=1))
    )
    assert result["status"] == "fault"
    assert result["label"] == "链路故障"

def test_readiness_cache_never_hides_a_fresh_stall(monkeypatch):
    """A fresh stall verdict must always win over a cached ready verdict.

    Regression guard (falsified in the release gate): the ready-heartbeat cache
    backs the *busy fallback* only.  Serving it before probing hid fresh stalls
    for the TTL window and broke fail-closed determinism.
    """
    import services.ocr_service as ocr

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-qwen-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://qwen.example.invalid/v1")
    ocr._local_image_readiness_cache = None
    ocr._local_image_readiness_cache_at = 0.0
    ocr._local_image_readiness_probe = None
    ocr._local_image_readiness_probe_loop = None

    monkeypatch.setattr(
        ocr, "get_local_image_readiness", lambda: {"ready": True, "reason": ""}
    )
    first = asyncio.run(ocr.get_ocr_readiness())
    assert first["ready"] is True

    monkeypatch.setattr(
        ocr,
        "get_local_image_readiness",
        lambda: {"ready": False, "reason": "local_image_pipeline_stalled"},
    )
    second = asyncio.run(ocr.get_ocr_readiness())
    assert second["ready"] is False
    assert second["reason"] == "local_image_pipeline_stalled"
