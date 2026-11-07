import asyncio
import os
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from models_user import User
from routers import grade
from routers import grade_pipeline
from routers import grade_template
from services import (
    analyzer,
    template_column_layout,
    model_concurrency,
    ocr_service,
    ocr_teacher_fairness,
)
from services.quick_grader import QuickBatch
from tests.grade_family import set_grade_global
from routers import grade_availability
from routers import grade_recognition
from routers import grade_template_availability
from services import template_column_layout_provider
from services import template_column_layout_recognize


class _LocalLimiter:
    pass


def _install_ready_chain(
    monkeypatch,
    *,
    ocr_status="available",
    hardware_status="ready",
):
    from services import flash_grading

    hardware_calls = []
    async def ocr_ready():
        return {"configured": True, "ready": True, "reason": ""}

    async def capacity(_teacher_id):
        return {"status": ocr_status, "label": "可用"}

    monkeypatch.setattr(ocr_service, "get_ocr_readiness", ocr_ready)
    monkeypatch.setattr(
        flash_grading,
        "get_flash_readiness",
        lambda: {"configured": True, "ready": True, "reason": ""},
    )
    monkeypatch.setattr(
        ocr_teacher_fairness, "public_teacher_availability", capacity
    )
    monkeypatch.setattr(model_concurrency, "get_model_limiter", lambda: _LocalLimiter())

    async def storage_ready(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    set_grade_global(monkeypatch, "get_upload_storage_readiness_async", storage_ready)

    async def hardware_ready(_teacher_id, **contract):
        hardware_calls.append(contract)
        return {
            "status": hardware_status,
            "message": f"hardware:{hardware_status}",
        }

    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", hardware_ready)
    monkeypatch.setattr(grade_template_availability, "get_public_hardware_readiness", hardware_ready)
    return hardware_calls


def test_batch_grade_availability_reports_whole_chain_ready(monkeypatch):
    hardware_calls = _install_ready_chain(monkeypatch)

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state == {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
    }
    assert hardware_calls == []


@pytest.mark.parametrize(
    "hardware_status",
    [
        "waiting_accelerator",
        "unavailable",
        "fault",
    ],
)
def test_batch_grade_availability_ignores_accelerator_hardware(
    monkeypatch,
    hardware_status,
):
    hardware_calls = _install_ready_chain(
        monkeypatch,
        hardware_status=hardware_status,
    )

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state["status"] == "available"
    assert "hardware" not in state
    assert hardware_calls == []


def test_plain_grading_gate_does_not_probe_accelerator(monkeypatch):
    hardware_calls = _install_ready_chain(
        monkeypatch,
        hardware_status="unavailable",
    )

    state = asyncio.run(
        grade._require_batch_grade_chain(SimpleNamespace(id=7))
    )

    assert state["status"] == "available"
    assert hardware_calls == []


def test_template_grading_gate_requires_full_column_accelerator(monkeypatch):
    hardware_calls = _install_ready_chain(
        monkeypatch,
        hardware_status="unavailable",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            grade._require_batch_grade_chain(
                SimpleNamespace(id=7),
                requires_accelerator=True,
                accelerator_stage=grade_template.STUDENT_MULTI_EVIDENCE_STAGE,
                accelerator_artifact_contract=(
                    grade_template.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
                ),
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["status"] == "unavailable"
    assert hardware_calls == [{
        "stage": grade_template.STUDENT_MULTI_EVIDENCE_STAGE,
        "artifact_contract": grade_template.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    }]


def test_accelerator_gate_rejects_implicit_legacy_contract(monkeypatch):
    _install_ready_chain(monkeypatch)

    with pytest.raises(ValueError, match="stage and artifact contract"):
        asyncio.run(
            grade._require_batch_grade_chain(
                SimpleNamespace(id=7),
                requires_accelerator=True,
            )
        )


def test_completed_ocr_scoring_gate_skips_image_dependencies_but_checks_flash(
    tmp_path,
    monkeypatch,
):
    from services import flash_grading

    batch = _grading_batch(tmp_path)
    storage_calls = []
    layout_checks = []

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("completed OCR was gated by an image-stage dependency")

    async def checkpoint_storage(path, byte_budget, *, required_copies):
        storage_calls.append((path, byte_budget, required_copies))
        if byte_budget >= grade_availability.MAX_UPLOAD_TOTAL_SIZE or required_copies != 1:
            return {
                "ready": False,
                "status": "unavailable",
                "code": "upload_storage_insufficient",
            }
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(ocr_service, "get_ocr_readiness", forbidden)
    monkeypatch.setattr(ocr_teacher_fairness, "public_teacher_availability", forbidden)
    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", forbidden)
    monkeypatch.setattr(grade_template_availability, "get_public_hardware_readiness", forbidden)
    set_grade_global(monkeypatch, "get_upload_storage_readiness_async", checkpoint_storage)
    monkeypatch.setattr(
        model_concurrency,
        "get_model_limiter",
        lambda: _LocalLimiter(),
    )

    def flash_ready():
        layout_checks.append(True)
        return {"configured": True, "ready": True, "reason": ""}

    monkeypatch.setattr(
        flash_grading,
        "get_flash_readiness",
        flash_ready,
    )

    state = asyncio.run(grade._require_batch_scoring_chain(batch))

    assert state["status"] == "available"
    assert storage_calls == [(batch.batch_dir, 1, 1)]
    assert layout_checks == [True]


def test_template_confirm_ignores_client_answers_before_scoring_readiness(
    tmp_path,
    monkeypatch,
):
    batch = _grading_batch(tmp_path)
    payload = _confirm_request().model_dump()
    payload["confirmed_answers"] = [{
        "question_no": "1", "content": "1+1=?", "answer": "3", "question_type": "fill",
    }]
    request = grade.ConfirmReq.model_validate(payload)
    # Client-sent answers are dropped at the request boundary.
    assert "confirmed_answers" not in request.model_dump()
    scoring_calls = []

    monkeypatch.setattr(QuickBatch, "load_meta", lambda *_args: batch)

    async def scoring_fault(candidate):
        scoring_calls.append(candidate.batch_id)
        return {"status": "fault", "message": "数学批改协调服务故障"}

    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", scoring_fault)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(grade.batch_grade_confirm(
            request,
            user=SimpleNamespace(id=7),
            db=_ReadOnlyDB(),
        ))

    assert exc.value.status_code == 503
    assert exc.value.detail["message"] == "数学批改协调服务故障"
    assert scoring_calls == [batch.batch_id]

    frozen = grade._authoritative_answers_for_confirmation(batch, request.category)
    assert [answer.standard_answer for answer in frozen] == ["2"]


def test_confirm_uses_scoring_gate_for_template_batch_after_ocr(
    tmp_path,
    monkeypatch,
):
    batch = _grading_batch(tmp_path)
    before = copy.deepcopy(batch.to_dict())
    scoring_calls = []

    async def scoring_fault(candidate):
        scoring_calls.append(candidate.batch_id)
        return {"status": "fault", "message": "数学批改协调服务故障"}

    async def image_gate_forbidden(*_args, **_kwargs):
        raise AssertionError("completed OCR was gated by image-stage readiness")

    monkeypatch.setattr(QuickBatch, "load_meta", lambda *_args: batch)
    monkeypatch.setattr(batch, "save_meta", lambda: pytest.fail("metadata was mutated"))
    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", scoring_fault)
    monkeypatch.setattr(grade_availability, "batch_grade_availability", image_gate_forbidden)
    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", image_gate_forbidden)
    monkeypatch.setattr(grade_template_availability, "get_public_hardware_readiness", image_gate_forbidden)
    monkeypatch.setattr(ocr_service, "get_ocr_readiness", image_gate_forbidden)
    monkeypatch.setattr(
        ocr_teacher_fairness,
        "public_teacher_availability",
        image_gate_forbidden,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(grade.batch_grade_confirm(
            _confirm_request(),
            user=SimpleNamespace(id=7),
            db=_ReadOnlyDB(),
        ))

    assert exc.value.status_code == 503
    assert exc.value.detail["message"] == "数学批改协调服务故障"
    assert scoring_calls == [batch.batch_id]
    assert batch.to_dict() == before


def test_completed_ocr_scoring_gate_still_fails_closed_for_flash(
    tmp_path,
    monkeypatch,
):
    from services import flash_grading

    batch = _grading_batch(tmp_path)

    async def checkpoint_storage(*_args, **_kwargs):
        return {"ready": True, "status": "available", "code": "ready"}

    set_grade_global(monkeypatch, "get_upload_storage_readiness_async", checkpoint_storage)
    monkeypatch.setattr(model_concurrency, "get_model_limiter", lambda: _LocalLimiter())
    monkeypatch.setattr(
        flash_grading,
        "get_flash_readiness",
        lambda: {"configured": True, "ready": False, "reason": "invalid"},
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(grade._require_batch_scoring_chain(batch))

    assert exc.value.status_code == 503
    assert exc.value.detail["status"] == "unavailable"
    assert "数学批改" in exc.value.detail["message"]


def test_batch_grade_availability_preserves_teacher_queue_state(monkeypatch):
    _install_ready_chain(monkeypatch, ocr_status="queued")

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state["status"] == "queued"
    assert state["label"] == "需排队"
    assert "人数" in state["message"]


@pytest.mark.parametrize("missing_layer", ["ocr", "qwen3.8-flash"])
def test_batch_grade_availability_fails_closed_when_model_layer_is_unready(
    monkeypatch,
    missing_layer,
):
    _install_ready_chain(monkeypatch)

    if missing_layer == "ocr":
        async def ocr_unready():
            return {"configured": False, "ready": False, "reason": "disabled"}

        monkeypatch.setattr(ocr_service, "get_ocr_readiness", ocr_unready)
    else:
        from services import flash_grading
        monkeypatch.setattr(flash_grading, "get_flash_readiness", lambda: {
            "configured": True, "ready": False, "reason": "invalid",
        })

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state["status"] == "unavailable"
    assert state["label"] == "批改不可用"
    assert "管理员" in state["message"]


def test_batch_grade_availability_reports_coordinator_fault(monkeypatch):
    _install_ready_chain(monkeypatch)

    class _BrokenRedisLimiter(model_concurrency.RedisModelLimiter):
        async def ping(self):
            raise TimeoutError("redis unavailable")

    limiter = _BrokenRedisLimiter(None)
    monkeypatch.setattr(model_concurrency, "get_model_limiter", lambda: limiter)

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state["status"] == "fault"
    assert state["label"] == "链路故障"
    assert "协调" in state["message"]


def test_batch_grade_availability_has_one_bounded_readiness_budget(monkeypatch):
    _install_ready_chain(monkeypatch)

    async def stalled_ocr():
        await asyncio.Event().wait()

    monkeypatch.setattr(ocr_service, "get_ocr_readiness", stalled_ocr)
    monkeypatch.setattr(grade_availability, "READINESS_IO_TIMEOUT_SECONDS", 0.01)

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    # Contract change (readiness/busy decoupling): one bounded budget stays,
    # but budget expiry under load is now the honest "queued" state instead
    # of a chain fault — a starved probe means busy, not broken.  Real
    # coordination faults (see the coordinator-fault test above) stay fault.
    assert state["status"] == "queued"


def test_batch_grade_availability_runs_independent_probes_concurrently(monkeypatch):
    _install_ready_chain(monkeypatch)
    started = set()

    async def exercise():
        release = asyncio.Event()

        async def wait_for_all(name, result):
            started.add(name)
            if len(started) == 3:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=0.25)
            return result

        async def storage(*_args, **_kwargs):
            return await wait_for_all(
                "storage",
                {"ready": True, "status": "available", "code": "ready"},
            )

        async def ocr():
            return await wait_for_all(
                "models",
                {"configured": True, "ready": True, "reason": ""},
            )

        async def capacity(_teacher_id):
            return await wait_for_all(
                "capacity",
                {"status": "available", "label": "可用"},
            )

        set_grade_global(monkeypatch, "get_upload_storage_readiness_async", storage)
        monkeypatch.setattr(ocr_service, "get_ocr_readiness", ocr)
        monkeypatch.setattr(
            ocr_teacher_fairness,
            "public_teacher_availability",
            capacity,
        )
        monkeypatch.setattr(grade_availability, "READINESS_IO_TIMEOUT_SECONDS", 0.5)
        return await grade.batch_grade_availability(User(id=7, role="teacher"))

    state = asyncio.run(exercise())

    assert state["status"] == "available"
    assert started == {"storage", "models", "capacity"}


def test_batch_grade_availability_blocks_new_work_when_storage_is_low(monkeypatch):
    _install_ready_chain(monkeypatch)

    async def storage_low(*_args, **_kwargs):
        return {
            "ready": False,
            "status": "unavailable",
            "code": "upload_storage_insufficient",
        }

    set_grade_global(monkeypatch, "get_upload_storage_readiness_async", storage_low)

    state = asyncio.run(grade.batch_grade_availability(User(id=7, role="teacher")))

    assert state["status"] == "unavailable"
    assert state["label"] == "批改不可用"
    assert "存储空间不足" in state["message"]


def test_template_availability_adds_layout_model_without_blocking_plain_grading(monkeypatch):
    hardware_calls = _install_ready_chain(monkeypatch)

    async def layout_unready(_limiter=None):
        return {
            "configured": True,
            "ready": False,
            "status": "unavailable",
            "reason": "all_credentials_invalid",
        }

    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_unready)

    user = User(id=7, role="teacher")
    plain = asyncio.run(grade.batch_grade_availability(user))
    template = asyncio.run(grade_template.batch_grade_template_availability(user))

    assert plain["status"] == "available"
    assert "hardware" not in plain
    assert template["status"] == "available"
    assert template["label"] == "可用"
    assert template["remote_layout_status"] == "unavailable"
    assert template["local_layout_fallback"] == "local_gutter_v1"
    assert template["hardware"]["status"] == "ready"
    assert {item["stage"] for item in hardware_calls} == {
        "student_multi_evidence",
    }


def test_template_availability_maps_hardware_wait_to_queue(monkeypatch):
    _install_ready_chain(monkeypatch, hardware_status="waiting_accelerator")

    async def layout_ready(_limiter=None):
        return {"configured": True, "ready": True, "status": "available"}

    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_ready)

    state = asyncio.run(
        grade_template.batch_grade_template_availability(User(id=7, role="teacher"))
    )

    # Template correction and frozen-column materialization run locally.  The
    # student-sheet accelerator is informational here and cannot queue import.
    assert state["status"] == "available"
    assert state["hardware"]["status"] == "ready"
    assert state["registration_hardware"]["status"] == "waiting_accelerator"


@pytest.mark.parametrize("hardware_status", ["ready", "waiting_accelerator", "unavailable", "fault"])
def test_template_registration_hardware_is_explicitly_advisory(monkeypatch, hardware_status):
    calls = _install_ready_chain(monkeypatch, hardware_status=hardware_status)

    async def layout_ready(_limiter=None):
        return {"configured": True, "ready": True, "status": "available"}

    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_ready)
    user = User(id=7, role="teacher")
    state = asyncio.run(grade_template.batch_grade_template_availability(user))
    assert state["registration_hardware"]["status"] == hardware_status
    assert state["registration_hardware"]["advisory"] is True
    assert state["status"] == "available"
    assert asyncio.run(grade._require_batch_grade_chain(user))["status"] == "available"
    assert len(calls) == 1  # Ordinary recognition does not require a node probe.


def test_template_availability_ignores_retired_template_hardware_stages(monkeypatch):
    calls = _install_ready_chain(monkeypatch)

    async def hardware(_teacher_id, **contract):
        calls.append(contract)
        status = (
            "unavailable"
            if contract.get("stage") == "template_ocr_evidence"
            else "ready"
        )
        return {"status": status, "message": f"hardware:{status}"}

    async def layout_ready(_limiter=None):
        return {"configured": True, "ready": True, "status": "available"}

    monkeypatch.setattr(grade_availability, "get_public_hardware_readiness", hardware)
    monkeypatch.setattr(grade_template_availability, "get_public_hardware_readiness", hardware)
    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_ready)

    state = asyncio.run(
        grade_template.batch_grade_template_availability(User(id=7, role="teacher"))
    )

    assert state["status"] == "available"
    assert state["hardware"]["status"] == "ready"
    assert state["registration_hardware"]["status"] == "ready"
    assert {item["stage"] for item in calls} == {
        "student_multi_evidence",
    }


def test_column_layout_readiness_fails_closed_on_invalid_credentials(monkeypatch):
    class InvalidPool:
        def enabled_credentials(self):
            return []

        def readiness(self):
            return {
                "configured": True,
                "ready": False,
                "configured_keys": 1,
                "enabled_keys": 0,
                "immediately_available_keys": 0,
            }

    monkeypatch.setattr(
        template_column_layout_recognize,
        "_resolve_template_key_pool",
        lambda: InvalidPool(),
    )
    monkeypatch.setattr(
        template_column_layout_provider,
        "_resolve_template_key_pool",
        lambda: InvalidPool(),
    )
    monkeypatch.setattr(template_column_layout_provider, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")
    monkeypatch.setattr(
        template_column_layout_provider,
        "TEMPLATE_CUT_BASE_URL",
        "https://mimo.example.invalid/v1",
    )

    state = asyncio.run(
        template_column_layout.get_column_layout_readiness(_LocalLimiter())
    )

    assert state["configured"] is True
    assert state["ready"] is False
    assert state["status"] == "unavailable"
    assert state["reason"] == "all_credentials_invalid"


def test_template_gate_keeps_local_layout_fallback_when_layout_model_is_unready(monkeypatch):
    async def core_ready(_user):
        return {"status": "available", "message": "ready"}

    async def layout_unready(_limiter=None):
        return {"status": "unavailable", "ready": False}

    monkeypatch.setattr(grade_availability, "batch_grade_availability", core_ready)
    monkeypatch.setattr(grade_availability, "_batch_grade_service_availability", core_ready)
    monkeypatch.setattr(grade_template_availability, "_batch_grade_service_availability", core_ready)
    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_unready)
    user = SimpleNamespace(id=7)

    assert asyncio.run(grade._require_batch_grade_chain(user))["status"] == "available"
    state = asyncio.run(grade_template._require_template_recognition_chain(user))

    assert state["status"] == "available"
    assert state["template_layout_status"] == "unavailable"
    assert state["template_layout_fallback"] == "local_gutter_v1"


def test_template_gate_ignores_only_hardware_overlay_for_client_candidate(
    monkeypatch,
):
    async def core_ready(_user):
        return {"status": "available", "message": "模型和存储可用"}

    async def hardware_probe_forbidden(_user):
        raise AssertionError("template candidate gate queried accelerator readiness")

    async def layout_ready(_limiter=None):
        return {"status": "available", "ready": True}

    monkeypatch.setattr(grade_availability, "_batch_grade_service_availability", core_ready)
    monkeypatch.setattr(grade_template_availability, "_batch_grade_service_availability", core_ready)
    monkeypatch.setattr(grade_availability, "batch_grade_availability", hardware_probe_forbidden)
    monkeypatch.setattr(grade_template_availability, "get_column_layout_readiness", layout_ready)

    state = asyncio.run(
        grade_template._require_template_recognition_chain(SimpleNamespace(id=7))
    )

    assert state["status"] == "available"


class _NeverReadUpload:
    filename = "paper.png"

    def __init__(self):
        self.read_calls = 0

    async def read(self, *_args, **_kwargs):
        self.read_calls += 1
        raise AssertionError("an unavailable request must not read upload content")


class _ReadOnlyDB:
    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None

    def add(self, *_args, **_kwargs):
        raise AssertionError("readiness failure must not write to the database")

    def flush(self):
        raise AssertionError("readiness failure must not flush the database")

    def commit(self):
        raise AssertionError("readiness failure must not commit the database")

    def rollback(self):
        pass

    def close(self):
        pass


async def _fault_availability(_user):
    return {
        "status": "fault",
        "label": "链路故障",
        "message": "批改链路暂时故障",
    }


@pytest.mark.parametrize("entrypoint", ["recognize", "template_recognize"])
def test_new_recognition_rejects_unavailable_chain_before_upload_side_effects(
    monkeypatch,
    entrypoint,
):
    upload = _NeverReadUpload()
    directory_calls = []
    monkeypatch.setattr(grade_availability, "batch_grade_availability", _fault_availability)
    monkeypatch.setattr(
        grade_availability,
        "_batch_grade_service_availability",
        _fault_availability,
    )
    monkeypatch.setattr(grade_template_availability,
        "_batch_grade_service_availability",
        _fault_availability,
    )
    monkeypatch.setattr(
        os,
        "makedirs",
        lambda *args, **kwargs: directory_calls.append((args, kwargs)),
    )

    if entrypoint == "recognize":
        operation = grade.batch_grade_recognize(
            files=[upload],
            category="daily",
            exam_name="",
            template_id="confirmed-template",
            mode="precise",
            user=SimpleNamespace(id=7, current_class_id=9, education_stage="senior"),
            db=_ReadOnlyDB(),
        )
    else:
        operation = grade_template.batch_grade_template_recognize(
            files=[upload],
            canonical_files=None,
            canonical_metadata=None,
            has_answers=True,
            category="daily",
            exam_name="",
            user=SimpleNamespace(id=7, current_class_id=9, education_stage="senior"),
            db=_ReadOnlyDB(),
        )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(operation)

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "error": "GRADING_CHAIN_UNAVAILABLE",
        "status": "fault",
        "message": "批改链路暂时故障",
    }
    assert upload.read_calls == 0
    assert directory_calls == []


def _grading_batch(tmp_path, *, status="ocr_done"):
    batch = QuickBatch("readiness-gate", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 9
    batch.source_mode = "template"
    batch.template_id = "readiness-template"
    batch.status = status
    paper_questions = [{
        "question_no": "1",
        "question_type": "fill",
        "student_answer": "2",
        "answer_state": "visible",
    }]
    batch.papers = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(tmp_path / "paper.png"),
        "orientation_status": "auto_passed",
        "identity": {"status": "needs_confirmation"},
        "questions": paper_questions,
    }]
    batch.questions = [{
        "question_no": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
    }]
    batch.answer_map = {
        "1": {
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
        },
    }
    return batch


def _confirm_request():
    return grade.ConfirmReq(
        batch_id="readiness-gate",
        mode="precise",
        category="daily",
        identity_resolutions=[{
            "paper_id": "paper-1",
            "action": "create",
            "confirmed_name": "学生甲",
            "confirmed_student_no": "01",
        }],
    )


def test_confirm_returns_503_before_freezing_or_writing_when_chain_is_faulted(
    tmp_path,
    monkeypatch,
):
    batch = _grading_batch(tmp_path)
    before = copy.deepcopy(batch.to_dict())
    save_calls = []
    monkeypatch.setattr(QuickBatch, "load_meta", lambda *_args: batch)
    monkeypatch.setattr(batch, "save_meta", lambda: save_calls.append(True))
    monkeypatch.setattr(
        grade_availability,
        "_batch_grade_scoring_availability",
        lambda _batch: _fault_availability(None),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(grade.batch_grade_confirm(
            _confirm_request(),
            user=SimpleNamespace(id=7),
            db=_ReadOnlyDB(),
        ))

    assert exc.value.status_code == 503
    assert exc.value.detail["status"] == "fault"
    assert batch.to_dict() == before
    assert save_calls == []


async def _consume_stream(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_confirm_rechecks_readiness_before_first_grading_side_effect(
    tmp_path,
    monkeypatch,
):
    batch = _grading_batch(tmp_path)
    before = copy.deepcopy(batch.to_dict())
    readiness_calls = 0

    async def availability(_user):
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            return {"status": "available", "message": "ready"}
        return {"status": "fault", "message": "协调服务故障"}

    monkeypatch.setattr(QuickBatch, "load_meta", lambda *_args: batch)
    monkeypatch.setattr(batch, "save_meta", lambda: pytest.fail("metadata was mutated"))
    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", availability)
    monkeypatch.setattr(
        grade_availability,
        "batch_grade_availability",
        lambda _user: pytest.fail("grading rechecked the OCR/upload chain"),
    )
    monkeypatch.setattr(
        grade_availability,
        "get_public_hardware_readiness",
        lambda *_args, **_kwargs: pytest.fail("grading rechecked accelerator hardware"),
    )
    monkeypatch.setattr(grade_template_availability,
        "get_public_hardware_readiness",
        lambda *_args, **_kwargs: pytest.fail("grading rechecked accelerator hardware"),
    )
    monkeypatch.setattr(grade_pipeline, "SessionLocal", _ReadOnlyDB)
    monkeypatch.setattr(grade_recognition, "SessionLocal", _ReadOnlyDB)
    set_grade_global(monkeypatch, "batch_operation_lock", lambda *_args, **_kwargs: nullcontext())

    async def exercise():
        response = await grade.batch_grade_confirm(
            _confirm_request(),
            user=SimpleNamespace(id=7),
            db=_ReadOnlyDB(),
        )
        return await _consume_stream(response)

    body = asyncio.run(exercise())

    error_payload = json.loads(
        next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    )
    assert {key: error_payload[key] for key in ("code", "status", "message")} == {
        "code": "GRADING_CHAIN_UNAVAILABLE",
        "status": "fault",
        "message": "协调服务故障",
    }
    assert error_payload["error_info"]["stage"] == "grading"
    assert len(error_payload["error_info"]["correlation_id"]) == 32
    assert readiness_calls == 2
    assert batch.to_dict() == before


def test_completed_confirm_replay_does_not_require_live_chain(tmp_path, monkeypatch):
    batch = _grading_batch(tmp_path, status="review_ready")
    request = _confirm_request()
    confirmed = grade._authoritative_answers_for_confirmation(batch, request.category)
    identity_plan = {
        "paper-1": {
            "action": "create",
            "student_id": None,
            "name": "学生甲",
            "student_no": "01",
        },
    }
    batch.grading_contract = grade._build_grading_contract(
        mode=request.mode,
        category=request.category,
        exam_name=request.exam_name,
        confirmed=confirmed,
        identity_plan=identity_plan,
    )
    batch.grading_contract_hash = grade._grading_contract_hash(
        batch.grading_contract
    )
    batch.results = [{
        "paper_id": "paper-1",
        "review_id": "paper-1",
        "student_name": "学生甲",
        "score": 10,
        "max_score": 10,
    }]
    batch.review_results = [{"paper_id": "paper-1", "review_id": "paper-1"}]
    before = copy.deepcopy(batch.to_dict())

    async def must_not_check(_batch):
        raise AssertionError("completed replay must not depend on the live chain")

    monkeypatch.setattr(QuickBatch, "load_meta", lambda *_args: batch)
    monkeypatch.setattr(batch, "save_meta", lambda: pytest.fail("replay wrote metadata"))
    monkeypatch.setattr(grade_availability, "_batch_grade_scoring_availability", must_not_check)
    monkeypatch.setattr(grade_availability, "batch_grade_availability", must_not_check)
    monkeypatch.setattr(grade_pipeline, "SessionLocal", _ReadOnlyDB)
    monkeypatch.setattr(grade_recognition, "SessionLocal", _ReadOnlyDB)
    set_grade_global(monkeypatch, "batch_operation_lock", lambda *_args, **_kwargs: nullcontext())

    async def exercise():
        response = await grade.batch_grade_confirm(
            request,
            user=SimpleNamespace(id=7),
            db=_ReadOnlyDB(),
        )
        return await _consume_stream(response)

    body = asyncio.run(exercise())

    events = [line for line in body.splitlines() if line.startswith("event:")]
    payloads = [
        json.loads(line[5:])
        for line in body.splitlines()
        if line.startswith("data:")
    ]
    assert events == ["event: result", "event: done"]
    assert payloads[-1]["idempotent"] is True
    assert batch.to_dict() == before
