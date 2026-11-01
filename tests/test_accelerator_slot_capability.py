"""Synthetic old/new nodes across persisted jobs, real render and acceptance."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from tests.test_accelerator_slot_geometry import enqueue, _frozen_artifact
from models_accelerator import AcceleratorJob
from models_user import User
from services import accelerator_artifacts as artifacts
from services import accelerator_jobs as jobs
from services import accelerator_validation as validation
from desktop.worker.mathagent_worker.handoff import DesktopHandoffRunner, HandoffConfig
from desktop.worker.mathagent_worker.preprocessing import _materialize_candidate

SLOT_CONTRACT = "client-evidence-slot-geometry-v1"


def _heartbeat():
    runner = DesktopHandoffRunner(HandoffConfig(
        server="https://example.invalid", node_id="synthetic", token="synthetic"))
    return runner._heartbeat_payload()


def _node(db, *, modern=True, scope="platform"):
    node, _ = jobs.register_node(db, owner_user_id=1, name="synthetic", access_scope=scope)
    heartbeat = _heartbeat()
    if not modern:
        heartbeat["capabilities"]["artifact_contracts"] = [
            c for c in heartbeat["capabilities"]["artifact_contracts"] if c != SLOT_CONTRACT]
    jobs.heartbeat(db, node, **heartbeat)
    return node


def _legacy(artifact):
    artifact = copy.deepcopy(artifact)
    for item in artifact["question_schema"] + artifact["template_answer_regions"]:
        item.pop("slot_ids")
        item.pop("region_kinds")
    return artifact


def _render(tmp_path, artifact, payload):
    return _materialize_candidate(
        tmp_path / "student.png", tmp_path / artifact["template_image"], payload,
        {"status": "succeeded", "page_type_id": "page_1",
         "homography": [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
         "good_matches": 100, "inliers": 100, "inlier_ratio": 1.,
         "reprojection_rmse": 0., "worker_metrics": {"backend": "windows_cpu_opencv"}},
        tmp_path / "worker")


def test_worker_advertises_slot_contract_from_real_heartbeat():
    assert SLOT_CONTRACT in _heartbeat()["capabilities"]["artifact_contracts"]


@pytest.mark.parametrize("scope", ["tenant", "platform"])
def test_old_node_cannot_lease_slot_job(tmp_path, enqueue, scope):
    payload = enqueue(_frozen_artifact(tmp_path))
    with enqueue.Session() as db:
        node = _node(db, modern=False, scope=scope)
        assert jobs.lease_next_job(db, node) is None
        row = db.get(AcceleratorJob, payload["job_id"])
        assert row.status == "queued" and row.attempt_count == 0
        assert row.assigned_node_id is None


def test_old_platform_node_skips_slot_only_teacher_without_starving_legacy(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    slot = enqueue(artifact)
    legacy = enqueue(_legacy(artifact))
    with enqueue.Session() as db:
        db.add(User(id=2, name="second synthetic", role="teacher", status="active"))
        row = db.get(AcceleratorJob, legacy["job_id"])
        row.teacher_id = 2
        db.commit()
        node = _node(db, modern=False)
        leased, _ = jobs.lease_next_job(db, node)
        assert leased.id == legacy["job_id"]
        assert db.get(AcceleratorJob, slot["job_id"]).attempt_count == 0


def _upload(row, result, paths):
    for entry, path in zip(result["artifact_manifest"]["evidence_images"], paths):
        target = artifacts.candidate_artifact_path(row.source_path, row.id, entry["kind"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


@pytest.mark.parametrize("modern,slots", [(True, True), (True, False), (False, False)])
@pytest.mark.parametrize("server_replay", [False, True])
def test_real_worker_result_can_be_uploaded_received_and_validated(tmp_path, enqueue, modern, slots, server_replay):
    artifact = _frozen_artifact(tmp_path)
    if not slots:
        artifact = _legacy(artifact)
    payload = enqueue(artifact)
    with enqueue.Session() as db:
        node = _node(db, modern=modern)
        row, secret = jobs.lease_next_job(db, node)
        assert row.id == payload["job_id"]
        result, _, _, paths = _render(tmp_path, artifact, payload)
        if slots:
            assert result["slot_geometry_receipt"] == {
                "contract_version": SLOT_CONTRACT,
                "layout_snapshot_sha256": payload["layout_snapshot_sha256"],
                "labels": {slot: box for item in artifact["template_answer_regions"]
                           for slot, box in zip(item["slot_ids"], item["regions"])},
            }
        else:
            assert "slot_geometry_receipt" not in result
        if server_replay:
            result.pop("local_quality_report")
        _upload(row, result, paths)
        accepted = jobs.submit_result(db, node, job_id=row.id, lease_secret=secret, result=result)
        assert accepted.status == "candidate_received"
        final = validation.validate_candidate_job(db, row.id)
        assert final.status == "validated", final.server_validation_json
        assert final.server_validation_status == "accepted"
        report = json.loads(final.server_validation_json)
        assert report["server_evidence_rendered"] is server_replay
        assert report["server_source_transform_replayed"] is server_replay
        assert json.loads(final.layout_snapshot_json) == payload["layout_snapshot"]


@pytest.mark.parametrize("bad_receipt", ["missing", "wrong_version", "wrong_hash", "extra", "swapped", "dropped"])
def test_slot_submission_rejects_legacy_or_unbound_receipt(tmp_path, enqueue, bad_receipt):
    artifact = _frozen_artifact(tmp_path)
    payload = enqueue(artifact)
    with enqueue.Session() as db:
        node = _node(db)
        row, secret = jobs.lease_next_job(db, node)
        result, _, _, _ = _render(tmp_path, artifact, payload)
        receipt = copy.deepcopy(result["slot_geometry_receipt"])
        if bad_receipt == "missing":
            result.pop("slot_geometry_receipt", None)
        else:
            if bad_receipt == "wrong_version":
                receipt["contract_version"] = "client-evidence-columns-v1"
            elif bad_receipt == "wrong_hash":
                receipt["layout_snapshot_sha256"] = "0" * 64
            elif bad_receipt == "swapped":
                labels = receipt["labels"]
                labels["2[3]"], labels["2[1]"] = labels["2[1]"], labels["2[3]"]
            elif bad_receipt == "dropped":
                receipt["labels"].pop("2[3]")
            else:
                receipt["unexpected"] = True
            result["slot_geometry_receipt"] = receipt
        with pytest.raises(ValueError, match="slot geometry"):
            jobs.submit_result(db, node, job_id=row.id, lease_secret=secret, result=result)
        db.refresh(row)
        assert row.status == "leased" and row.candidate_json is None


def test_persisted_pre_gate_candidate_cannot_bypass_slot_validation(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    payload = enqueue(artifact)
    with enqueue.Session() as db:
        node = _node(db)
        row, secret = jobs.lease_next_job(db, node)
        result, _, _, paths = _render(tmp_path, artifact, payload)
        _upload(row, result, paths)
        # Simulate a genuine pre-gate pending row, bypassing only receipt POST:
        # all image bytes, frozen JSON and candidate are real worker outputs.
        result.pop("slot_geometry_receipt", None)
        row.candidate_json = jobs.canonical_json(result)
        row.candidate_sha256 = jobs.sha256_json(result)
        row.status = "candidate_received"
        row.server_validation_status = "pending"
        db.commit()
        final = validation.validate_candidate_job(db, row.id)
        assert final.status == "fallback_failed"
        report = json.loads(final.server_validation_json)
        assert report["reason"] == "client_evidence_contract_rejected"
        assert report["server_evidence_rendered"] is False
        assert not list(tmp_path.glob("evidence-set-*"))


def test_http_lease_upload_and_result_boundary_with_real_worker_payload(tmp_path, enqueue):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from database import get_db
    from routers import accelerator

    artifact = _frozen_artifact(tmp_path)
    enqueued = enqueue(artifact)
    with enqueue.Session() as db:
        node, token = jobs.register_node(db, owner_user_id=1, name="http-synthetic", access_scope="platform")
        headers = {"X-Accelerator-Node": node.id, "X-Accelerator-Token": token}

    def db_dependency():
        with enqueue.Session() as db:
            yield db

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = db_dependency
    with TestClient(app) as client:
        heartbeat = _heartbeat()
        old_heartbeat = copy.deepcopy(heartbeat)
        old_heartbeat["capabilities"]["artifact_contracts"].remove(SLOT_CONTRACT)
        response = client.post("/api/accelerator/nodes/heartbeat", headers=headers, json=old_heartbeat)
        assert response.status_code == 200, response.text
        response = client.post("/api/accelerator/jobs/lease", headers=headers)
        assert response.status_code == 200 and response.json()["job"] is None
        assert client.post("/api/accelerator/nodes/heartbeat", headers=headers, json=heartbeat).status_code == 200
        response = client.post("/api/accelerator/jobs/lease", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()["job"]
        assert payload["job_id"] == enqueued["job_id"]
        result, _, _, paths = _render(tmp_path, artifact, payload)
        old_result = copy.deepcopy(result)
        old_result.pop("slot_geometry_receipt")
        url = f"/api/accelerator/jobs/{payload['job_id']}"
        response = client.post(url + "/result", headers=headers, json={
            "lease_id": payload["lease_id"], "result": old_result})
        assert response.status_code == 422 and "slot geometry" in response.text
        for entry, path in zip(result["artifact_manifest"]["evidence_images"], paths):
            response = client.put(url + "/artifacts/" + entry["kind"],
                headers={**headers, "X-Accelerator-Lease": payload["lease_id"], "Content-Type": "image/png"},
                content=path.read_bytes())
            assert response.status_code == 200, response.text
        for _ in range(2):  # The exact receipt remains idempotent.
            response = client.post(url + "/result", headers=headers, json={
                "lease_id": payload["lease_id"], "result": result})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "validated", response.text
    with enqueue.Session() as db:
        row = db.get(AcceleratorJob, enqueued["job_id"])
        assert row.status == "validated"
        assert json.loads(row.candidate_json)["slot_geometry_receipt"] == result["slot_geometry_receipt"]
        assert json.loads(row.layout_snapshot_json) == enqueued["layout_snapshot"]


def test_capability_loss_after_lease_rejects_even_modern_receipt(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    payload = enqueue(artifact)
    with enqueue.Session() as db:
        node = _node(db)
        row, secret = jobs.lease_next_job(db, node)
        result, _, _, _ = _render(tmp_path, artifact, payload)
        heartbeat = _heartbeat()
        heartbeat["capabilities"]["artifact_contracts"] = [
            c for c in heartbeat["capabilities"]["artifact_contracts"] if c != SLOT_CONTRACT]
        jobs.heartbeat(db, node, **heartbeat)
        with pytest.raises(ValueError, match="slot geometry"):
            jobs.submit_result(db, node, job_id=row.id, lease_secret=secret, result=result)
        db.refresh(row)
        assert row.status == "leased" and row.candidate_json is None
