import asyncio
import tempfile
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from services import system_readiness
from services import accelerator_jobs_nodes
from services import accelerator_jobs_queue
from services import accelerator_jobs_leases


def test_hardware_readiness_distinguishes_ready_waiting_and_unavailable(
    monkeypatch,
):
    from services import accelerator_jobs

    now = datetime(2026, 8, 11, 12, 0, 0)
    rows = []

    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return list(rows)

    class FakeSession:
        def query(self, *_args):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr("database.SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(accelerator_jobs_queue, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_nodes, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_leases, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_nodes, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(accelerator_jobs_leases, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(accelerator_jobs, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(
        accelerator_jobs,
        "node_supports_stage",
        lambda node, stage: node.compatible and stage == "template_registration",
    )
    monkeypatch.setattr(
        accelerator_jobs_nodes,
        "node_supports_artifact_contract",
        lambda node, contract: (
            node.artifact_compatible and contract == "client-evidence-v1"
        ),
    )
    monkeypatch.setattr(
        accelerator_jobs,
        "node_supports_artifact_contract",
        lambda node, contract: (
            node.artifact_compatible and contract == "client-evidence-v1"
        ),
    )

    unavailable = system_readiness._read_accelerator_hardware_readiness(7)
    rows.append(SimpleNamespace(
        compatible=True,
        artifact_compatible=False,
        free_slots=1,
        last_seen_at=now,
    ))
    legacy_artifact = system_readiness._read_accelerator_hardware_readiness(7)
    rows[0].artifact_compatible = True
    rows[0].last_seen_at = now - timedelta(seconds=31)
    disconnected = system_readiness._read_accelerator_hardware_readiness(7)
    rows[0].last_seen_at = now
    rows[0].free_slots = 0
    waiting = system_readiness._read_accelerator_hardware_readiness(7)
    rows[0].free_slots = 1
    ready = system_readiness._read_accelerator_hardware_readiness(7)

    assert unavailable["status"] == "unavailable"
    assert legacy_artifact["status"] == "unavailable"
    assert disconnected["status"] == "unavailable"
    assert waiting["status"] == "waiting_accelerator"
    assert ready["status"] == "ready"
    for state in (unavailable, legacy_artifact, disconnected, waiting, ready):
        assert set(state) == {"status", "message"}


def test_hardware_readiness_filters_by_requested_stage_and_contract(monkeypatch):
    from services import accelerator_jobs

    now = datetime(2026, 8, 11, 12, 0, 0)
    rows = [SimpleNamespace(
        stages={"template_registration"},
        artifact_contracts={"client-evidence-v1"},
        free_slots=1,
        last_seen_at=now,
    )]

    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return list(rows)

    class FakeSession:
        def query(self, *_args):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr("database.SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(accelerator_jobs_queue, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_nodes, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_leases, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs, "utcnow", lambda: now)
    monkeypatch.setattr(accelerator_jobs_nodes, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(accelerator_jobs_leases, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(accelerator_jobs, "NODE_STALE_SECONDS", 30)
    monkeypatch.setattr(
        accelerator_jobs,
        "node_supports_stage",
        lambda node, stage: stage in node.stages,
    )
    monkeypatch.setattr(
        accelerator_jobs_nodes,
        "node_supports_artifact_contract",
        lambda node, contract: contract in node.artifact_contracts,
    )
    monkeypatch.setattr(
        accelerator_jobs,
        "node_supports_artifact_contract",
        lambda node, contract: contract in node.artifact_contracts,
    )

    registration = system_readiness._read_accelerator_hardware_readiness(7)
    canonicalization = system_readiness._read_accelerator_hardware_readiness(
        7,
        stage="template_canonicalization",
        artifact_contract="client-canonical-template-v1",
    )
    rows.append(SimpleNamespace(
        stages={"template_canonicalization"},
        artifact_contracts={"client-canonical-template-v1"},
        free_slots=0,
        last_seen_at=now,
    ))
    canonicalization_waiting = (
        system_readiness._read_accelerator_hardware_readiness(
            7,
            stage="template_canonicalization",
            artifact_contract="client-canonical-template-v1",
        )
    )

    assert registration["status"] == "ready"
    assert canonicalization["status"] == "unavailable"
    assert canonicalization_waiting["status"] == "waiting_accelerator"


def test_public_hardware_readiness_fails_closed_without_leaking_probe_details(
    monkeypatch,
):
    monkeypatch.setattr(
        system_readiness,
        "_read_accelerator_hardware_readiness",
        lambda _teacher_id, **_requirement: {
            "status": "ready",
            "message": "internal",
            "node_count": 99,
        },
    )

    state = asyncio.run(system_readiness.get_public_hardware_readiness(7))

    assert state == {
        "status": "ready",
        "message": "教师客户端或机会式加速节点已就绪。",
    }


def test_public_hardware_readiness_timeout_does_not_block_event_loop(monkeypatch):
    release = threading.Event()
    settled = threading.Event()

    def blocked(_teacher_id, **_requirement):
        release.wait(timeout=2)
        settled.set()
        return {"status": "ready"}

    monkeypatch.setattr(
        system_readiness,
        "_read_accelerator_hardware_readiness",
        blocked,
    )
    try:
        state = asyncio.run(system_readiness.get_public_hardware_readiness(
            7,
            timeout_seconds=0.01,
        ))
    finally:
        release.set()
        assert settled.wait(timeout=1)

    assert state["status"] == "fault"
    assert set(state) == {"status", "message"}


def test_public_hardware_probe_is_keyed_by_stage_and_contract(monkeypatch):
    calls = []

    def read(_teacher_id, *, stage, artifact_contract):
        calls.append((stage, artifact_contract))
        return {
            "status": (
                "ready"
                if stage == "template_registration"
                else "waiting_accelerator"
            )
        }

    monkeypatch.setattr(
        system_readiness,
        "_read_accelerator_hardware_readiness",
        read,
    )

    async def exercise():
        return await asyncio.gather(
            system_readiness.get_public_hardware_readiness(7),
            system_readiness.get_public_hardware_readiness(
                7,
                stage="template_canonicalization",
                artifact_contract="client-canonical-template-v1",
            ),
        )

    registration, canonicalization = asyncio.run(exercise())

    assert registration["status"] == "ready"
    assert canonicalization["status"] == "waiting_accelerator"
    assert calls == [
        ("template_registration", "client-evidence-v1"),
        ("template_canonicalization", "client-canonical-template-v1"),
    ]


def test_distinct_hardware_contracts_do_not_head_of_line_block(monkeypatch):
    started = 0
    started_lock = threading.Lock()
    all_started = threading.Event()

    def read(_teacher_id, *, stage, artifact_contract):
        nonlocal started
        with started_lock:
            started += 1
            if started == 3:
                all_started.set()
        if not all_started.wait(timeout=1):
            raise TimeoutError("distinct hardware probes were serialized")
        return {"status": "ready"}

    monkeypatch.setattr(
        system_readiness,
        "_read_accelerator_hardware_readiness",
        read,
    )

    async def exercise():
        return await asyncio.gather(
            system_readiness.get_public_hardware_readiness(
                7,
                stage="template_registration",
                artifact_contract="client-evidence-v1",
                timeout_seconds=0.5,
            ),
            system_readiness.get_public_hardware_readiness(
                7,
                stage="template_canonicalization",
                artifact_contract="client-canonical-template-v1",
                timeout_seconds=0.5,
            ),
            system_readiness.get_public_hardware_readiness(
                7,
                stage="template_ocr",
                artifact_contract="client-template-ocr-evidence-v1",
                timeout_seconds=0.5,
            ),
        )

    states = asyncio.run(exercise())

    assert started == 3
    assert [state["status"] for state in states] == ["ready", "ready", "ready"]


def test_matching_hardware_contracts_share_one_in_flight_probe(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    calls = 0

    def read(_teacher_id, **_requirement):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1)
        return {"status": "ready"}

    monkeypatch.setattr(
        system_readiness,
        "_read_accelerator_hardware_readiness",
        read,
    )

    async def exercise():
        tasks = [
            asyncio.create_task(
                system_readiness.get_public_hardware_readiness(
                    7,
                    timeout_seconds=0.5,
                )
            )
            for _ in range(3)
        ]
        assert await asyncio.to_thread(started.wait, 0.25)
        release.set()
        return await asyncio.gather(*tasks)

    states = asyncio.run(exercise())

    assert calls == 1
    assert [state["status"] for state in states] == ["ready", "ready", "ready"]


def test_upload_storage_budget_includes_staging_copies_and_reserve(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(system_readiness, "UPLOAD_STORAGE_RESERVE_BYTES", 50)
    monkeypatch.setattr(system_readiness.os, "access", lambda *_args: True)
    free_bytes = {"value": 249}
    monkeypatch.setattr(
        system_readiness.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=0, free=free_bytes["value"]),
    )

    blocked = system_readiness.get_upload_storage_readiness(
        tmp_path,
        100,
        required_copies=2,
    )
    free_bytes["value"] = 250
    ready = system_readiness.get_upload_storage_readiness(
        tmp_path,
        100,
        required_copies=2,
    )

    assert blocked == {
        "ready": False,
        "status": "unavailable",
        "code": "upload_storage_insufficient",
    }
    assert ready == {"ready": True, "status": "available", "code": "ready"}


def test_upload_storage_check_fails_closed_when_target_is_not_writable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(system_readiness.os, "access", lambda *_args: False)

    state = system_readiness.get_upload_storage_readiness(tmp_path, 100)

    assert state == {
        "ready": False,
        "status": "fault",
        "code": "upload_storage_not_writable",
    }


def test_upload_storage_check_rejects_file_where_directory_is_required(tmp_path):
    target = tmp_path / "uploads"
    target.write_bytes(b"not-a-directory")

    state = system_readiness.get_upload_storage_readiness(target, 100)

    assert state == {
        "ready": False,
        "status": "fault",
        "code": "upload_storage_path_invalid",
    }


def test_multipart_temp_dir_is_bound_to_monitored_upload_volume(tmp_path):
    previous = tempfile.tempdir
    try:
        upload_dir = tmp_path / "uploads"
        configured = system_readiness.configure_multipart_temp_dir(
            upload_dir
        )
        assert configured == str((upload_dir / ".multipart-tmp").resolve())
        assert tempfile.gettempdir() == configured
        assert not upload_dir.exists()
    finally:
        tempfile.tempdir = previous


def test_storage_probe_prepares_multipart_temp_dir(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(system_readiness, "UPLOAD_STORAGE_RESERVE_BYTES", 1)
    monkeypatch.setattr(
        system_readiness.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=0, free=1000),
    )

    state = system_readiness.get_upload_storage_readiness(upload_dir, 100)

    assert state == {"ready": True, "status": "available", "code": "ready"}
    spool = upload_dir / ".multipart-tmp"
    assert spool.is_dir()
    assert not spool.is_symlink()


def test_post_parse_storage_probe_does_not_create_nested_spool(tmp_path, monkeypatch):
    target = tmp_path / "batch"
    monkeypatch.setattr(system_readiness, "UPLOAD_STORAGE_RESERVE_BYTES", 1)
    monkeypatch.setattr(
        system_readiness.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=0, free=1000),
    )

    state = system_readiness.get_upload_storage_readiness(
        target,
        100,
        required_copies=1,
    )

    assert state == {"ready": True, "status": "available", "code": "ready"}
    assert not (target / ".multipart-tmp").exists()


def test_async_storage_readiness_times_out_without_blocking_event_loop(
    tmp_path,
    monkeypatch,
):
    release = threading.Event()

    def blocked(*_args, **_kwargs):
        release.wait(timeout=2)
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(system_readiness, "get_upload_storage_readiness", blocked)
    try:
        state = asyncio.run(system_readiness.get_upload_storage_readiness_async(
            tmp_path,
            100,
            timeout_seconds=0.01,
        ))
    finally:
        release.set()

    assert state == {
        "ready": False,
        "status": "fault",
        "code": "upload_storage_check_timed_out",
    }


def test_repeated_storage_timeouts_reuse_one_stalled_probe(tmp_path, monkeypatch):
    release = threading.Event()
    settled = threading.Event()
    calls = 0

    def blocked(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        release.wait(timeout=2)
        settled.set()
        return {"ready": True, "status": "available", "code": "ready"}

    monkeypatch.setattr(system_readiness, "get_upload_storage_readiness", blocked)

    async def exercise():
        return await asyncio.gather(*(
            system_readiness.get_upload_storage_readiness_async(
                tmp_path,
                100,
                timeout_seconds=0.01,
            )
            for _ in range(3)
        ))

    try:
        states = asyncio.run(exercise())
    finally:
        release.set()
        assert settled.wait(timeout=1)

    assert calls == 1
    assert states == [{
        "ready": False,
        "status": "fault",
        "code": "upload_storage_check_timed_out",
    }] * 3


@pytest.fixture
def grading_dependencies(monkeypatch):
    import config
    from services import analyzer, flash_grading, model_concurrency, ocr_service

    state = SimpleNamespace(
        storage={"ready": True},
        ocr={"configured": True, "ready": True},
        local={"configured": True, "ready": True},
        shared={"configured": True, "ready": True},
        calls=[],
        shared_error=None,
        ping_error=None,
    )

    async def storage(*_args, **_kwargs):
        state.calls.append("storage")
        return state.storage

    async def ocr():
        state.calls.append("ocr")
        return state.ocr

    class RedisLimiter:
        async def ping(self):
            state.calls.append("ping")
            if state.ping_error:
                raise state.ping_error

    state.redis = RedisLimiter()
    state.limiter = object()

    def local(provider):
        state.calls.append(provider + "_local")
        return state.local

    async def shared(provider, limiter):
        assert limiter is state.redis
        state.calls.append(provider + "_shared")
        if state.shared_error:
            raise state.shared_error
        return state.shared

    monkeypatch.setattr(config, "GRADING_PROVIDER", "qwen3.8-flash")
    monkeypatch.setattr(system_readiness, "get_upload_storage_readiness_async", storage)
    monkeypatch.setattr(ocr_service, "get_ocr_readiness", ocr)
    monkeypatch.setattr(model_concurrency, "RedisModelLimiter", RedisLimiter)
    monkeypatch.setattr(model_concurrency, "get_model_limiter", lambda: state.limiter)
    monkeypatch.setattr(flash_grading, "get_flash_readiness", lambda: local("flash"))
    monkeypatch.setattr(
        flash_grading, "get_flash_readiness_async", lambda limiter: shared("flash", limiter),
    )
    return state


@pytest.mark.parametrize("redis", [False, True])
def test_grading_readiness_checks_flash(
    monkeypatch, grading_dependencies, redis,
):
    state = grading_dependencies
    if redis:
        state.limiter = state.redis

    result = asyncio.run(system_readiness.get_grading_infrastructure_readiness("unused", 1))

    assert result == {"ready": True, "status": "available", "code": "ready"}
    expected = ["storage"] + (["ping"] if redis else []) + ["ocr"]
    expected += ["flash_local"] + (["flash_shared"] if redis else [])
    assert state.calls == expected


@pytest.mark.parametrize("provider,code", [
    ("qwen3.8-flash", "flash_unavailable"),
])
@pytest.mark.parametrize("redis", [False, True])
@pytest.mark.parametrize("unavailable", [
    {}, {"configured": False, "ready": True}, {"configured": True, "ready": False},
])
def test_grading_readiness_fails_closed_for_selected_pool(
    monkeypatch, grading_dependencies, provider, code, redis, unavailable,
):
    import config

    state = grading_dependencies
    monkeypatch.setattr(config, "GRADING_PROVIDER", provider)
    if redis:
        # Shared invalidity/cooldown must override a healthy process-local pool.
        state.limiter = state.redis
        state.shared = unavailable
    else:
        state.local = unavailable

    result = asyncio.run(system_readiness.get_grading_infrastructure_readiness("unused", 1))

    assert result == {"ready": False, "status": "unavailable", "code": code}


@pytest.mark.parametrize("failure,code", [
    ("storage", "upload_storage_insufficient"),
    ("ocr", "ocr_unavailable"),
    ("ping", "grading_coordination_unavailable"),
    ("shared", "grading_coordination_unavailable"),
    ("timeout", "grading_readiness_timed_out"),
])
def test_flash_readiness_preserves_dependency_failures(
    monkeypatch, grading_dependencies, failure, code,
):
    import config

    state = grading_dependencies
    state.limiter = state.redis
    if failure == "storage":
        state.storage = {"ready": False, "status": "unavailable", "code": code}
    elif failure == "ocr":
        state.ocr = {"configured": True, "ready": False}
    elif failure == "ping":
        state.ping_error = ConnectionError("synthetic private coordinator detail")
    elif failure == "shared":
        state.shared_error = ConnectionError("synthetic private pool detail")
    elif failure == "timeout":
        state.shared_error = TimeoutError("synthetic readiness deadline")

    result = asyncio.run(system_readiness.get_grading_infrastructure_readiness("unused", 1))

    assert result == {
        "ready": False,
        "status": "unavailable" if failure in {"storage", "ocr"} else "fault",
        "code": code,
    }
    if failure == "storage":
        assert state.calls == ["storage"]


def test_flash_shared_readiness_deadline_cancels_stalled_probe(
    monkeypatch, grading_dependencies,
):
    from services import flash_grading

    state = grading_dependencies
    state.limiter = state.redis
    cancelled = []

    async def stalled(limiter):
        assert limiter is state.redis
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(flash_grading, "get_flash_readiness_async", stalled)
    monkeypatch.setattr(system_readiness, "READINESS_IO_TIMEOUT_SECONDS", 0.01)

    result = asyncio.run(system_readiness.get_grading_infrastructure_readiness("unused", 1))

    assert result == {
        "ready": False, "status": "fault", "code": "grading_readiness_timed_out",
    }
    assert cancelled == [True]
