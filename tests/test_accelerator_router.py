"""HTTP contract tests for the remote accelerator router."""
from __future__ import annotations

from contextlib import contextmanager
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models_user import User
from routers import accelerator
from routers.auth import _require_user


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'router.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add_all([
        User(id=1, name="管理员", role="admin", status="active"),
        User(id=2, name="教师", role="teacher", status="active"),
    ])
    db.commit()
    db.close()

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "source.jpg").write_bytes(b"source")
    (upload_dir / "template.jpg").write_bytes(b"template")
    monkeypatch.setattr(accelerator, "UPLOAD_DIR", str(upload_dir))

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db

    @contextmanager
    def client(user_id: int):
        role = "admin" if user_id == 1 else "teacher"
        app.dependency_overrides[_require_user] = lambda: User(id=user_id, name="u", role=role, status="active")
        with TestClient(app) as value:
            yield value

    return client


def _enqueue_payload():
    return {
        "paper_id": "paper-1",
        "source_relative_path": "source.jpg",
        "template_id": "template-1",
        "template_relative_path": "template.jpg",
        "layout_snapshot": {
            "page_type_id": "page_1",
            "answer_regions": [{
                "question_number": "1",
                "question_type": "solve",
                "answer_bbox": [0, 0, 1000, 1000],
            }],
        },
    }


def _result():
    return {
        "status": "succeeded",
        "page_type_id": "page_1",
        "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "good_matches": 100,
        "inliers": 90,
        "inlier_ratio": 0.9,
        "reprojection_rmse": 1.0,
        "coverage": 1.0,
        "answer_source_complete": True,
        "incomplete_answer_regions": [],
        "artifact_sha256": "a" * 64,
        "artifact_manifest": {
            "contract_version": "client-evidence-v1",
            "coordinate_space": "answer_region_evidence_montage_v1",
            "transform_graph": "global_homography_only_v1",
            "renderer": "template_evidence_v1",
            "source_width": 800,
            "source_height": 1000,
            "template_width": 800,
            "template_height": 1000,
            "evidence": {
                "sha256": "b" * 64,
                "byte_size": 100,
                "width": 800,
                "height": 1000,
                "channels": 3,
                "format": "png",
            },
        },
        "question_numbers": ["1"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {
            "ownership": "confirmed_bbox_neighbor_clamped",
            "answer_source_coverage": [{
                "question_number": "1",
                "covered_pixels": 800_000,
                "total_pixels": 800_000,
                "coverage": 1.0,
                "complete": True,
                "crop_box": [0, 0, 800, 1000],
            }],
            "incomplete_answer_regions": [],
        },
    }


def _png(*, mask: bool = False) -> bytes:
    output = io.BytesIO()
    Image.new("L" if mask else "RGB", (32, 32), 255).save(output, format="PNG")
    return output.getvalue()


def test_admin_registers_lists_revokes_and_token_pair_is_bound(api):
    with api(1) as client:
        registered = client.post("/api/accelerator/nodes/register", json={"name": "RTX node"})
        assert registered.status_code == 200
        body = registered.json()
        assert body["access_scope"] == "tenant"
        headers = {"X-Accelerator-Node": body["node_id"], "X-Accelerator-Token": body["node_token"]}
        heartbeat = client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={
                "capabilities": {"gpu": "RTX 4060", "batch_size": 8},
                "algorithm_version": body["algorithm_version"],
                "free_slots": 1,
                "queue_depth": 0,
                "estimated_finish_ms": 500,
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["estimated_finish_ms"] == 500
        assert len(client.get("/api/accelerator/nodes").json()["nodes"]) == 1
        assert client.post(f"/api/accelerator/nodes/{body['node_id']}/revoke").status_code == 200
        assert client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={"capabilities": {}, "algorithm_version": body["algorithm_version"], "free_slots": 1},
        ).status_code == 401


def test_admin_can_explicitly_register_platform_node(api):
    with api(1) as client:
        registered = client.post(
            "/api/accelerator/nodes/register",
            json={"name": "platform RTX node", "access_scope": "platform"},
        )
        assert registered.status_code == 200
        assert registered.json()["access_scope"] == "platform"
        listed = client.get("/api/accelerator/nodes").json()["nodes"]
        assert listed[0]["access_scope"] == "platform"


def test_server_execution_leases_only_image_processing_stages(
    api,
    monkeypatch,
):
    captured = []

    def fake_lease(_db, _node, *, requested_stages=None):
        captured.append(requested_stages)
        return None

    monkeypatch.setattr(accelerator, "lease_next_job", fake_lease)

    with api(1) as client:
        node = client.post(
            "/api/accelerator/nodes/register",
            json={"name": "image-only node", "access_scope": "platform"},
        ).json()
        headers = {
            "X-Accelerator-Node": node["node_id"],
            "X-Accelerator-Token": node["node_token"],
        }

        model_poll = client.post(
            "/api/accelerator/jobs/lease",
            headers=headers,
            json={"stages": ["vision_model_request"]},
        )
        assert model_poll.status_code == 200
        assert model_poll.json()["job"] is None
        assert captured == []

        image_poll = client.post(
            "/api/accelerator/jobs/lease",
            headers=headers,
            json={
                "stages": [
                    "template_registration",
                    "vision_model_request",
                ]
            },
        )
        assert image_poll.status_code == 200
        assert captured == [["template_registration"]]

        invalid_poll = client.post(
            "/api/accelerator/jobs/lease",
            headers=headers,
            json={"stages": ["unknown_stage"]},
        )
        assert invalid_poll.status_code == 422


def test_teacher_desktop_registration_rotation_and_revocation_are_owner_bound(api):
    with api(2) as client:
        registered = client.post(
            "/api/accelerator/desktop/devices/register",
            json={"device_id": "win-device-2", "name": "教师电脑"},
        )
        assert registered.status_code == 200
        first = registered.json()
        assert first["lane"] == "teacher_desktop"
        headers = {
            "X-Accelerator-Node": first["node_id"],
            "X-Accelerator-Token": first["node_token"],
        }
        assert client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={
                "capabilities": {"platform": "windows"},
                "algorithm_version": first["algorithm_version"],
                "free_slots": 1,
            },
        ).status_code == 200
        devices = client.get("/api/accelerator/desktop/devices").json()["devices"]
        assert [device["device_id"] for device in devices] == ["win-device-2"]
        rotated = client.post(
            f"/api/accelerator/desktop/devices/{first['node_id']}/rotate",
            json={"token_ttl_days": 30},
        )
        assert rotated.status_code == 200
        second = rotated.json()
        assert second["node_token"] != first["node_token"]
        assert client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={
                "capabilities": {},
                "algorithm_version": first["algorithm_version"],
                "free_slots": 1,
            },
        ).status_code == 401

    with api(1) as other:
        assert other.post(
            f"/api/accelerator/desktop/devices/{first['node_id']}/revoke"
        ).status_code == 404

    with api(2) as owner:
        assert owner.post(
            f"/api/accelerator/desktop/devices/{first['node_id']}/revoke"
        ).status_code == 200
        assert owner.post(
            "/api/accelerator/nodes/heartbeat",
            headers={
                "X-Accelerator-Node": first["node_id"],
                "X-Accelerator-Token": second["node_token"],
            },
            json={
                "capabilities": {},
                "algorithm_version": first["algorithm_version"],
                "free_slots": 1,
            },
        ).status_code == 401


def test_teacher_desktop_status_distinguishes_missing_and_current_device(api):
    with api(2) as client:
        assert client.get(
            "/api/accelerator/desktop/devices/missing-device"
        ).status_code == 404
        registered = client.post(
            "/api/accelerator/desktop/devices/register",
            json={"device_id": "win-status-device", "name": "教师电脑"},
        ).json()
        status = client.get(
            f"/api/accelerator/desktop/devices/{registered['node_id']}"
        )
        assert status.status_code == 200
        assert status.json()["node_id"] == registered["node_id"]
        assert status.json()["device_id"] == "win-status-device"
        assert status.json()["enabled"] is True
        assert "node_token" not in status.json()


def test_non_admin_cannot_enqueue_and_path_escape_is_rejected(api):
    with api(2) as client:
        assert client.post("/api/accelerator/jobs/enqueue", json=_enqueue_payload()).status_code == 403
    with api(1) as client:
        payload = _enqueue_payload()
        payload["source_relative_path"] = "../outside.jpg"
        assert client.post("/api/accelerator/jobs/enqueue", json=payload).status_code == 400


def test_http_candidate_is_automatically_server_validated(api, monkeypatch):
    with api(1) as client:
        node = client.post("/api/accelerator/nodes/register", json={"name": "node"}).json()
        headers = {"X-Accelerator-Node": node["node_id"], "X-Accelerator-Token": node["node_token"]}
        client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={
                "capabilities": {
                    "batch_size": 8,
                    "stages": ["template_registration"],
                    "artifact_contracts": ["client-evidence-v1"],
                },
                "algorithm_version": node["algorithm_version"],
                "free_slots": 1,
            },
        )
        enqueued = client.post("/api/accelerator/jobs/enqueue", json=_enqueue_payload())
        assert enqueued.status_code == 200
        leased = client.post("/api/accelerator/jobs/lease", headers=headers).json()["job"]
        assert leased["job_id"] == enqueued.json()["job_id"]
        assert leased["layout_snapshot"]["page_type_id"] == "page_1"
        asset = client.get(
            leased["source_url"],
            headers={**headers, "X-Accelerator-Lease": leased["lease_id"]},
        )
        assert asset.status_code == 200 and asset.content == b"source"

        from routers import accelerator as accelerator_router
        from services import accelerator_jobs

        def fake_validate(db, job_id):
            accelerator_jobs.begin_server_validation(db, job_id)
            return accelerator_jobs.finish_server_validation(
                db,
                job_id,
                accepted=True,
                validation={"source_mask_replayed": True},
            )

        monkeypatch.setattr(accelerator_router, "try_validate_candidate_job", fake_validate)

        received = client.post(
            f"/api/accelerator/jobs/{leased['job_id']}/result",
            headers=headers,
            json={"lease_id": leased["lease_id"], "result": _result()},
        )
        assert received.status_code == 200
        assert received.json()["status"] == "validated"
        assert received.json()["server_validation_status"] == "accepted"
        replay = client.post(
            f"/api/accelerator/jobs/{leased['job_id']}/result",
            headers=headers,
            json={"lease_id": leased["lease_id"], "result": _result()},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "validated"


def test_candidate_artifact_upload_is_png_only_and_lease_bound(api):
    with api(1) as client:
        node = client.post(
            "/api/accelerator/nodes/register",
            json={"name": "upload node"},
        ).json()
        headers = {
            "X-Accelerator-Node": node["node_id"],
            "X-Accelerator-Token": node["node_token"],
        }
        client.post(
            "/api/accelerator/nodes/heartbeat",
            headers=headers,
            json={
                "capabilities": {
                    "stages": ["template_registration"],
                    "artifact_contracts": ["client-evidence-v1"],
                },
                "algorithm_version": node["algorithm_version"],
                "free_slots": 1,
            },
        )
        job_id = client.post(
            "/api/accelerator/jobs/enqueue", json=_enqueue_payload()
        ).json()["job_id"]
        leased = client.post(
            "/api/accelerator/jobs/lease", headers=headers
        ).json()["job"]
        lease_headers = {
            **headers,
            "X-Accelerator-Lease": leased["lease_id"],
            "Content-Type": "image/png",
        }

        aligned = client.put(
            f"/api/accelerator/jobs/{job_id}/artifacts/aligned_page",
            headers=lease_headers,
            content=_png(),
        )
        mask = client.put(
            f"/api/accelerator/jobs/{job_id}/artifacts/source_mask",
            headers=lease_headers,
            content=_png(mask=True),
        )
        evidence = client.put(
            f"/api/accelerator/jobs/{job_id}/artifacts/evidence",
            headers=lease_headers,
            content=_png(),
        )

        assert aligned.status_code == 404
        assert mask.status_code == 404
        assert evidence.status_code == 200
        assert evidence.json()["channels"] == 3
        assert client.put(
            f"/api/accelerator/jobs/{job_id}/artifacts/evidence",
            headers={**lease_headers, "Content-Type": "application/octet-stream"},
            content=_png(),
        ).status_code == 415
        assert client.put(
            f"/api/accelerator/jobs/{job_id}/artifacts/evidence",
            headers={**lease_headers, "X-Accelerator-Lease": "x" * 32},
            content=_png(),
        ).status_code == 409
