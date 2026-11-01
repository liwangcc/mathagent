"""Queue availability must use the same slot capability as actual leases."""
import asyncio

import pytest

from tests.test_accelerator_slot_geometry import enqueue, _frozen_artifact
from tests.test_accelerator_slot_capability import _heartbeat, _legacy, SLOT_CONTRACT
from models_accelerator import AcceleratorJob
from services import accelerator_business as business
from services import accelerator_jobs as jobs


def _register(Session, *, lane, modern, columns=True):
    with Session() as db:
        node, _ = jobs.register_node(
            db, owner_user_id=1, name="synthetic-lane", lane=lane,
            device_id="synthetic-queue-device" if lane == "teacher_desktop" else None,
        )
        heartbeat = _heartbeat()
        contracts = heartbeat["capabilities"]["artifact_contracts"]
        if not modern:
            contracts.remove(SLOT_CONTRACT)
        if not columns:
            contracts.remove(business.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION)
        jobs.heartbeat(db, node, **heartbeat)
        return node.id


def _enqueue(tmp_path, artifact):
    return business._enqueue_template_pages(
        teacher_id=1, batch_id="synthetic-batch", paper_id="synthetic-paper",
        source_path=str(tmp_path / "student.png"), template_id="synthetic-template",
        artifacts={"page_1": artifact}, template_dir=str(tmp_path),
    )


def test_old_node_is_not_reported_available_and_does_not_start_wait(tmp_path, enqueue, monkeypatch):
    artifact = _frozen_artifact(tmp_path)
    _register(enqueue.Session, lane="teacher_desktop", modern=False)

    async def no_wait(*_args, **_kwargs):
        raise AssertionError("old slot-incapable node must not spend remote wait budget")

    monkeypatch.setattr(business, "_wait_for_evidence", no_wait)
    result = asyncio.run(business.try_accelerated_evidence(
        teacher_id="1", batch_id="synthetic-batch", paper_id="synthetic-paper",
        source_path=str(tmp_path / "student.png"), template_id="synthetic-template",
        artifacts={"page_1": artifact}, template_dir=str(tmp_path),
    ))
    assert result.state == "unavailable"
    assert result.reason_code == "accelerator_node_unavailable"
    assert len(result.job_ids) == 1
    with enqueue.Session() as db:
        assert db.get(AcceleratorJob, result.job_ids[0]).attempt_count == 0


def test_slot_job_skips_old_desktop_and_chooses_capable_web_lane(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    old_id = _register(enqueue.Session, lane="teacher_desktop", modern=False)
    new_id = _register(enqueue.Session, lane="web_gpu", modern=True)
    from models_accelerator import AcceleratorNode
    result = _enqueue(tmp_path, artifact)
    assert result.node_available is True
    assert result.lane == "web_gpu"
    with enqueue.Session() as db:
        assert jobs.lease_next_job(db, db.get(AcceleratorNode, old_id)) is None
        leased, _ = jobs.lease_next_job(db, db.get(AcceleratorNode, new_id))
        assert leased.id == result.job_ids[0]


def test_legacy_job_still_uses_old_desktop_lane(tmp_path, enqueue):
    artifact = _legacy(_frozen_artifact(tmp_path))
    _register(enqueue.Session, lane="teacher_desktop", modern=False)
    result = _enqueue(tmp_path, artifact)
    assert result.node_available is True and result.lane == "teacher_desktop"


def test_slot_capability_alone_does_not_replace_column_artifact_capability(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    _register(enqueue.Session, lane="teacher_desktop", modern=True, columns=False)
    result = _enqueue(tmp_path, artifact)
    assert result.node_available is False
