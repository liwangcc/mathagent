"""Real-socket HTTP exercise for the accelerator router and reference worker."""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
import pytest
from fastapi import FastAPI
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models_accelerator import AcceleratorJob
from models_user import User
from routers import accelerator
from routers.auth import _require_user
from services import accelerator_jobs as jobs
from services import accelerator_business as business
from worker import accelerator_client


def _image(path: Path, *, student: bool = False):
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 920, 80):
        draw.line((60, y, 740, y), fill="black", width=3)
    for x in range(80, 760, 80):
        draw.rectangle((x, 40, x + 25, 65), outline="black", width=2)
    draw.text((90, 110), "MATH AGENT TEMPLATE 2026", fill="black")
    if student:
        draw.line((120, 260, 500, 320), fill="black", width=8)
    image.save(path)


class _BoundHttpContext(SimpleNamespace):
    def __repr__(self):
        # Pytest assertion introspection must not print even ephemeral tokens.
        return "<synthetic bound-node HTTP context>"


@pytest.fixture
def bound_nodes_http(tmp_path, monkeypatch, request):
    """Real HTTP with tenant devices or explicitly registered platform nodes."""
    access_scope = getattr(request, "param", "tenant")
    assert access_scope in {"tenant", "platform"}
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bound-nodes.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    bindings = []
    with Session() as db:
        db.add_all([
            User(id=1, name="Synthetic teacher A", role="teacher", status="active"),
            User(id=2, name="Synthetic teacher B", role="teacher", status="active"),
            User(id=3, name="Synthetic platform owner", role="admin", status="active"),
        ])
        db.commit()
        for index, (owner, device) in enumerate(((1, "desktop-a"), (1, "desktop-b"), (2, "foreign-desktop"))):
            platform = access_scope == "platform" and index < 2
            node, token = jobs.register_node(
                db,
                owner_user_id=3 if platform else owner,
                name=device,
                access_scope="platform" if platform else "tenant",
                lane="web_gpu" if platform else "teacher_desktop",
                device_id=None if platform else device,
                token_ttl_days=1,
            )
            bindings.append({
                "X-Accelerator-Node": node.id,
                "X-Accelerator-Token": token,
            })

    def override_db():
        with Session() as db:
            yield db

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(accelerator, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(business, "SessionLocal", Session)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started
    port = next(iter(server.servers)).sockets[0].getsockname()[1]
    clients = [
        httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30, trust_env=False)
        for headers in bindings
    ]
    try:
        for client in clients:
            response = client.post("/api/accelerator/nodes/heartbeat", json={
                "algorithm_version": jobs.ALGORITHM_VERSION,
                "free_slots": 8,
                "capabilities": {
                    "hardware_ready": True,
                    "backend": "windows_cpu_opencv",
                    "lease_slots": 8,
                    "stages": ["student_multi_evidence"],
                    "artifact_contracts": ["client-evidence-columns-v1"],
                },
            })
            assert response.status_code == 200, response.text
        yield _BoundHttpContext(
            Session=Session, clients=clients, bindings=bindings,
            access_scope=access_scope,
            job_lane="web_gpu" if access_scope == "platform" else "teacher_desktop",
        )
    finally:
        for client in clients:
            client.close()
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()


def _column_layout(page_type="page_1", question="1"):
    return {
        "page_type_id": page_type,
        "question_numbers": [question],
        "question_schema": [{"question_number": question, "question_type": "solve"}],
        "question_anchors": [{
            "question_number": question,
            "question_type": "solve",
            "question_number_bbox": [60, 200, 100, 230],
        }],
        "blank_detection_enabled": False,
    }


def _enqueue_columns(Session, source, template, *, paper_id, owner=1, page_type="page_1", question="1", lane="teacher_desktop"):
    from services.template_anchor_crops import build_anchor_crop_plan

    layout = _column_layout(page_type, question)
    layout["question_crop_plan"] = build_anchor_crop_plan(
        {"column_layout": {
            "column_count": 1,
            "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        }},
        artifact_sha256=hashlib.sha256(template.read_bytes()).hexdigest(),
    )
    with Session() as db:
        job = jobs.enqueue_job(
            db,
            teacher_id=owner,
            batch_id="synthetic-two-node-batch",
            paper_id=paper_id,
            source_path=str(source),
            template_id=f"two-page-template:{page_type}",
            template_path=str(template),
            layout_snapshot=layout,
            stage="student_multi_evidence",
            lane=lane,
        )
        return job.id


def _lease_http(client):
    response = client.post("/api/accelerator/jobs/lease", json={"stages": ["student_multi_evidence"]})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def test_two_bound_nodes_race_one_batch_without_duplicate_or_foreign_leases(bound_nodes_http, tmp_path):
    """The race is at the real router/database CAS, not a mocked queue."""
    context = bound_nodes_http
    source, template = tmp_path / "source.png", tmp_path / "template.png"
    _image(template)
    _image(source, student=True)
    job_ids = {
        _enqueue_columns(context.Session, source, template, paper_id=f"page-{index}")
        for index in range(8)
    }
    foreign_id = _enqueue_columns(context.Session, source, template, paper_id="foreign-page", owner=2)
    barrier = threading.Barrier(2)

    def claim_four(client):
        claimed = []
        for _ in range(4):
            barrier.wait(timeout=10)
            claimed.append(_lease_http(client))
        return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_four, client) for client in context.clients[:2]]
        by_node = [future.result(timeout=30) for future in futures]
    leased = [job for node_jobs in by_node for job in node_jobs]
    assert len(leased) == 8 and all(leased)
    assert {job["job_id"] for job in leased} == job_ids
    assert len({job["lease_id"] for job in leased}) == 8
    assert all(len(node_jobs) == 4 for node_jobs in by_node)
    assert _lease_http(context.clients[0]) is None
    assert _lease_http(context.clients[1]) is None
    assert _lease_http(context.clients[2])["job_id"] == foreign_id

    own = by_node[0][0]
    assert context.clients[0].get(own["source_url"], headers={"X-Accelerator-Lease": own["lease_id"]}).status_code == 200
    for foreign in context.clients[1:]:
        assert foreign.get(own["source_url"], headers={"X-Accelerator-Lease": own["lease_id"]}).status_code == 409
        assert foreign.post(f"/api/accelerator/jobs/{own['job_id']}/renew", json={"lease_id": own["lease_id"]}).status_code == 409
        assert foreign.put(
            f"/api/accelerator/jobs/{own['job_id']}/artifacts/evidence_01",
            content=source.read_bytes(),
            headers={"Content-Type": "image/png", "X-Accelerator-Lease": own["lease_id"]},
        ).status_code == 409
    # A valid token from the other device must not authenticate this node ID.
    assert context.clients[0].post(
        "/api/accelerator/jobs/lease",
        json={},
        headers={"X-Accelerator-Token": context.bindings[1]["X-Accelerator-Token"]},
    ).status_code == 401
    with context.Session() as db:
        stored = db.query(AcceleratorJob).filter(AcceleratorJob.id.in_(job_ids)).all()
        assert {row.batch_id for row in stored} == {"synthetic-two-node-batch"}
        assert {row.teacher_id for row in stored} == {1}
        assert {row.attempt_count for row in stored} == {1}
        assert {row.assigned_node_id for row in stored} == {
            binding["X-Accelerator-Node"] for binding in context.bindings[:2]
        }


def _compute_http_columns(client, job, root):
    from desktop.worker.mathagent_worker.preprocessing import process_candidate

    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind in ("source", "template"):
        response = client.get(job[f"{kind}_url"], headers={"X-Accelerator-Lease": job["lease_id"]})
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == job[f"{kind}_sha256"]
        target = root / f"{kind}.png"
        target.write_bytes(response.content)
        paths.append(target)
    return process_candidate(
        *paths,
        job,
        artifact_output_dir=root / "processed",
        allow_gpu=False,
        allow_cpu_fallback=True,
    )


def _submit_http_columns(client, job, outcome):
    for index, path in enumerate(outcome.evidence_paths, 1):
        response = client.put(
            f"/api/accelerator/jobs/{job['job_id']}/artifacts/evidence_{index:02d}",
            content=path.read_bytes(),
            headers={"Content-Type": "image/png", "X-Accelerator-Lease": job["lease_id"]},
        )
        assert response.status_code == 200, response.text
    response = client.post(
        f"/api/accelerator/jobs/{job['job_id']}/result",
        json={"lease_id": job["lease_id"], "result": outcome.candidate},
    )
    assert response.status_code == 200, response.text
    # Both a completed synchronous receipt and a durable asynchronous receipt
    # are valid. Await the actual stored terminal state, never HTTP 200 alone.
    for _ in range(200):
        state = response.json()
        if state["status"] in {"validated", "validation_failed", "expired"}:
            return state
        time.sleep(0.025)
        response = client.post(
            f"/api/accelerator/jobs/{job['job_id']}/result",
            json={"lease_id": job["lease_id"], "result": outcome.candidate},
        )
        assert response.status_code == 200, response.text
    pytest.fail(f"result did not reach terminal acceptance: {state['status']}")


def _page_image(path, page_type, *, student=0):
    """Two visibly different printed pages, with separate handwriting per student."""
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    rng = random.Random(715 + page_type)
    draw.text((80, 40), f"SYNTHETIC MATH PAGE TYPE {page_type}", fill="black", font_size=25)
    draw.text((60, 200), f"{page_type}. Solve the printed problem below.", fill="black", font_size=20)
    for index in range(170):
        x, y = rng.randint(35, 725), rng.randint(90, 945)
        if 270 < y < 540:
            continue
        width, height = rng.randint(8, 34), rng.randint(8, 28)
        if page_type == 1:
            draw.rectangle((x, y, x + width, y + height), outline="black", width=2)
            draw.line((x, y, x + width, y + height), fill="black", width=2)
        else:
            draw.ellipse((x, y, x + width, y + height), outline="black", width=2)
            draw.line((x, y + height, x + width, y), fill="black", width=2)
        draw.text((x, y - 12), str(index + page_type * 200), fill="black")
    if student:
        # The answer zones intentionally differ by student so a crossed result
        # cannot pass the pixel preservation assertion merely by page identity.
        y = 330 + student * 55
        draw.line((150, y, 470, y + 30), fill="black", width=8)
        draw.line((470, y + 30, 560, y - 20), fill="black", width=8)
    image.save(path)


@pytest.mark.parametrize("bound_nodes_http", ["tenant", "platform"], indirect=True)
def test_completed_receipt_is_device_bound_idempotent_and_old_lease_cannot_return(bound_nodes_http, tmp_path):
    context = bound_nodes_http
    source, template = tmp_path / "source.png", tmp_path / "template.png"
    _page_image(template, 1)
    _page_image(source, 1, student=1)
    job_id = _enqueue_columns(
        context.Session, source, template, paper_id="late-page", lane=context.job_lane,
    )
    first = _lease_http(context.clients[0])
    assert first["job_id"] == job_id
    outcome = _compute_http_columns(context.clients[0], first, tmp_path / "first-attempt")
    # The first device gives the task back. A second bound device receives a
    # new lease; an in-flight response from device A must not overwrite it.
    response = context.clients[0].post(f"/api/accelerator/jobs/{job_id}/fail", json={
        "lease_id": first["lease_id"], "error_code": "simulated_connection_lost",
    })
    assert response.status_code == 200 and response.json()["status"] == "queued"
    second = _lease_http(context.clients[1])
    assert second["job_id"] == job_id and second["lease_id"] != first["lease_id"]
    result_path = f"/api/accelerator/jobs/{job_id}/result"
    old_result = {"lease_id": first["lease_id"], "result": outcome.candidate}
    assert context.clients[0].post(result_path, json=old_result).status_code == 409
    state = _submit_http_columns(context.clients[1], second, outcome)
    assert state["status"] == "validated", state
    assert state["server_validation"]["validator"] == "bound_client_full_column_v2"
    assert state["server_validation"]["node_access_scope"] == context.access_scope
    for client in (context.clients[0], context.clients[2]):
        # Even disclosure of another device's lease does not grant ownership.
        assert client.post(result_path, json={
            "lease_id": second["lease_id"], "result": outcome.candidate,
        }).status_code == 409
    duplicate = context.clients[1].post(result_path, json={
        "lease_id": second["lease_id"], "result": outcome.candidate,
    })
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "validated"
    assert context.clients[0].post(result_path, json=old_result).status_code == 409
    with context.Session() as db:
        stored = db.get(AcceleratorJob, job_id)
        assert stored.attempt_count == 2
        assert stored.assigned_node_id == context.bindings[1]["X-Accelerator-Node"]
        assert stored.server_validation_status == "accepted"


@pytest.mark.parametrize("bound_nodes_http", ["tenant", "platform"], indirect=True)
def test_consumed_and_accepted_superseded_receipts_remain_device_bound(bound_nodes_http, tmp_path):
    """Closing business work must not turn a successful delayed receipt into 409.

    These intentionally compatible synthetic layouts exercise receipt cleanup,
    not page-type ranking (which has its own strength/ambiguity tests below).
    """
    context = bound_nodes_http
    source, template = tmp_path / "source.png", tmp_path / "template.png"
    _page_image(template, 1)
    _page_image(source, 1, student=1)
    job_ids = [
        _enqueue_columns(
            context.Session, source, template, paper_id="receipt-siblings",
            page_type=f"page_{page}", question=str(page), lane=context.job_lane,
        )
        for page in (1, 2, 3)
    ]
    # Leases go oldest first, ties broken by the random job id, and on Windows
    # utcnow() moves in ~1 ms steps: the three jobs often share one creation
    # time. Give them distinct ones, in enqueue order, so the leases follow it.
    with context.Session() as db:
        created = db.get(AcceleratorJob, job_ids[0]).created_at
        for offset, job_id in enumerate(job_ids):
            db.get(AcceleratorJob, job_id).created_at = created + timedelta(milliseconds=offset)
        db.commit()
    attempts = []
    for index in range(3):
        client = context.clients[index % 2]
        job = _lease_http(client)
        assert job["job_id"] == job_ids[index]
        outcome = _compute_http_columns(client, job, tmp_path / f"attempt-{index}")
        if index < 2:
            state = _submit_http_columns(client, job, outcome)
            assert state["status"] == "validated"
            assert state["server_validation_status"] == "accepted"
        attempts.append((client, job, outcome))

    handle = business.AcceleratorEvidence(
        evidence=None,
        job_ids=tuple(job_ids),
        used_job_id=job_ids[0],
        state="ready",
    )
    assert business.finish_business_preprocessing(handle, fallback_succeeded=True) == 3
    assert business.finish_business_preprocessing(handle, fallback_succeeded=True) == 0

    for expected_status, (client, job, outcome) in zip(("consumed", "superseded"), attempts[:2]):
        path = f"/api/accelerator/jobs/{job['job_id']}/result"
        payload = {"lease_id": job["lease_id"], "result": outcome.candidate}
        for _ in range(2):
            response = client.post(path, json=payload)
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt["job_id"] == job["job_id"]
            assert receipt["status"] == expected_status
            assert receipt["server_validation_status"] == "accepted"
            assert receipt["server_artifact_ready"] is False
        for foreign in context.clients:
            if foreign is not client:
                assert foreign.post(path, json=payload).status_code == 409

    client, job, outcome = attempts[2]
    assert client.post(
        f"/api/accelerator/jobs/{job['job_id']}/result",
        json={"lease_id": job["lease_id"], "result": outcome.candidate},
    ).status_code == 409
    assert client.get(
        job["source_url"], headers={"X-Accelerator-Lease": job["lease_id"]},
    ).status_code == 409
    with context.Session() as db:
        for index, (_, job, _) in enumerate(attempts):
            stored = db.get(AcceleratorJob, job["job_id"])
            assert stored.status == ("consumed" if index == 0 else "superseded")
            if index < 2:
                assert stored.assigned_node_id == context.bindings[index]["X-Accelerator-Node"]
                assert stored.lease_id_hash
                assert stored.server_validation_status == "accepted"
            else:
                assert stored.assigned_node_id is None and stored.lease_id_hash is None
                assert stored.server_validation_status != "accepted"


@pytest.mark.parametrize("bound_nodes_http", ["tenant", "platform"], indirect=True)
def test_two_students_two_page_types_keep_correct_evidence_after_reverse_completion(bound_nodes_http, tmp_path):
    """Actual CPU node preprocessing + socket upload + actual business selection.

    GPU is not required for this deterministic protocol/pixel test. This does
    not pretend to exercise two physical computers or any real student data.
    """
    import numpy as np

    context = bound_nodes_http
    templates = {}
    for page in (1, 2):
        templates[page] = tmp_path / f"template-page-{page}.png"
        _page_image(templates[page], page)
    groups = {}
    for student in (1, 2):
        for source_page in (1, 2):
            paper = f"student-{student}-source-page-{source_page}"
            source = tmp_path / f"{paper}.png"
            _page_image(source, source_page, student=student)
            groups[paper] = {
                "expected_page": f"page_{source_page}",
                "question": str(source_page),
                "student": student,
                "ids": [
                    _enqueue_columns(
                        context.Session, source, templates[page], paper_id=paper,
                        page_type=f"page_{page}", question=str(page), lane=context.job_lane,
                    )
                    for page in (1, 2)
                ],
            }
    leased = []
    for index in range(8):
        client = context.clients[index % 2]
        leased.append((client, _lease_http(client)))
    assert len({job["job_id"] for _, job in leased}) == 8
    assert all(job["batch_id"] == "synthetic-two-node-batch" for _, job in leased)
    assert all(job["lane"] == context.job_lane for _, job in leased)
    correct = []
    incorrect = []
    for client, job in leased:
        group = groups[job["paper_id"]]
        (correct if job["layout_snapshot"]["page_type_id"] == group["expected_page"] else incorrect).append((client, job))

    # Correct pages arrive in reverse student/page order while every competing
    # template candidate remains running. A partial race must not choose yet.
    accepted_attempts = {}
    for client, job in reversed(correct):
        outcome = _compute_http_columns(client, job, tmp_path / f"work-{job['job_id']}")
        assert outcome.candidate["local_quality_report"]["version"] == "local-full-column-check-v2"
        state = _submit_http_columns(client, job, outcome)
        assert state["status"] == "validated", state
        validation = state["server_validation"]
        assert validation["validator"] == "bound_client_full_column_v2"
        assert validation["node_access_scope"] == context.access_scope
        assert validation["server_image_decode_performed"] is False
        assert validation["server_source_transform_replayed"] is False
        assert validation["server_evidence_rendered"] is False
        accepted_attempts[job["job_id"]] = (client, job, outcome)
        partial = business._inspect_evidence_once(groups[job["paper_id"]]["ids"])
        assert partial[0] is None, "page-type selection cannot depend on which node returns first"

    # Mismatched printed pages must fail on the external node, before upload.
    # Let the real retry policy close each rejected candidate rather than
    # mutating its DB status into a fabricated terminal success.
    for client, job in reversed(incorrect):
        with pytest.raises((RuntimeError, ValueError)):
            _compute_http_columns(client, job, tmp_path / f"wrong-{job['job_id']}")
        while True:
            response = client.post(f"/api/accelerator/jobs/{job['job_id']}/fail", json={
                "lease_id": job["lease_id"], "error_code": "page_type_mismatch",
            })
            assert response.status_code == 200, response.text
            if response.json()["status"] != "queued":
                break
            retried = _lease_http(client)
            assert retried["job_id"] == job["job_id"]
            job = retried

    accepted_evidence = []
    for paper, group in groups.items():
        evidence, selected_id, terminal, in_progress = business._inspect_evidence_once(group["ids"])
        assert evidence is not None, paper
        assert evidence.page_type_id == group["expected_page"]
        assert evidence.question_numbers == [group["question"]]
        assert selected_id in group["ids"] and not terminal and not in_progress
        assert evidence.diagnostics["accelerator"]["server_source_transform_replayed"] is False
        client, job, outcome = accepted_attempts[selected_id]
        assert [Path(path).read_bytes() for path in evidence.evidence_paths] == [
            path.read_bytes() for path in outcome.evidence_paths
        ]
        with Image.open(evidence.evidence_paths[0]) as image:
            rgb = np.asarray(image.convert("RGB"))
        y = 330 + group["student"] * 55
        assert (rgb[y - 5:y + 40, 150:470].max(axis=2) < 100).sum() > 700
        other_y = 330 + (3 - group["student"]) * 55
        assert (rgb[other_y - 5:other_y + 40, 150:470].max(axis=2) < 100).sum() < 100
        # Preserve a test-only evidence copy before normal business consumption
        # removes the transient accepted artifact directory.
        evidence_copy = tmp_path / "accepted-evidence" / paper / "evidence_01.png"
        evidence_copy.parent.mkdir(parents=True)
        evidence_copy.write_bytes(Path(evidence.evidence_paths[0]).read_bytes())
        handle = business.AcceleratorEvidence(
            evidence=evidence, job_ids=tuple(group["ids"]),
            used_job_id=selected_id, state="ready",
        )
        assert business.finish_business_preprocessing(handle, fallback_succeeded=True) == len(group["ids"])
        result_path = f"/api/accelerator/jobs/{selected_id}/result"
        payload = {"lease_id": job["lease_id"], "result": outcome.candidate}
        for _ in range(2):
            response = client.post(result_path, json=payload)
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt["status"] == "consumed"
            assert receipt["server_validation_status"] == "accepted"
        for foreign in context.clients:
            if foreign is not client:
                assert foreign.post(result_path, json=payload).status_code == 409
        accepted_evidence.append({
            "paper_id": paper,
            "page_type_id": evidence.page_type_id,
            "question_numbers": evidence.question_numbers,
            "selected_job_id": selected_id,
            "source_image": f"{paper}.png",
            "template_image": f"template-page-{group['question']}.png",
            "accepted_evidence": str(evidence_copy.relative_to(tmp_path)),
            "handwriting_preserved": True,
            "other_student_handwriting_absent": True,
            "server_source_transform_replayed": False,
            "local_evidence_bytes_preserved": True,
            "consumed_receipt_idempotent": True,
            "cross_node_receipt_rejected": True,
        })
    (tmp_path / "acceptance.json").write_text(json.dumps({
        "fixture_kind": "synthetic_two_nodes_two_students_two_page_types",
        "transport": "real_local_http_socket",
        "node_compute": "actual_desktop_cpu_preprocessing",
        "node_access_scope": context.access_scope,
        "validator": "bound_client_full_column_v2",
        "completed_pages": accepted_evidence,
    }, indent=2), encoding="utf-8")


def _seed_accepted_selection_candidate(Session, job_id, root, *, inliers, ratio):
    """Persisted-state fixture for winner ranking, not an image-acceptance test."""
    root.mkdir(parents=True)
    evidence_path = root / "evidence_01.png"
    Image.new("RGB", (800, 1000), "white").save(evidence_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps({
        "contract_version": "client-evidence-columns-v1",
        "evidence_images": [{
            "kind": "evidence_01",
            "byte_size": evidence_path.stat().st_size,
            "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")
    with Session() as db:
        job = db.get(AcceleratorJob, job_id)
        layout = json.loads(job.layout_snapshot_json)
        job.status = "validated"
        job.server_validation_status = "accepted"
        job.candidate_json = json.dumps({
            "inliers": inliers,
            "inlier_ratio": ratio,
            "coverage": 1.0,
            "reprojection_rmse": 0.5,
            "good_matches": round(inliers / ratio),
        })
        job.server_validation_json = json.dumps({
            "validator": "bound_client_full_column_v2",
            "bound_node_identity_validated": True,
            "local_quality_report_validated": True,
            "client_answer_source_complete": True,
            "server_source_transform_replayed": False,
            "server_evidence_rendered": False,
            "page_type_id": layout["page_type_id"],
            "question_numbers": layout["question_numbers"],
            "blank_question_numbers": [],
            "evidence_diagnostics": {},
        })
        job.server_artifact_path = str(manifest_path)
        job.server_artifact_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        db.commit()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_page_type_winner_uses_match_strength_not_receipt_or_job_order(bound_nodes_http, tmp_path, ambiguous):
    context = bound_nodes_http
    source, template = tmp_path / "source.png", tmp_path / "template.png"
    _image(source, student=True)
    _image(template)
    weak = _enqueue_columns(context.Session, source, template, paper_id="same-source", page_type="page_1", question="1")
    strong = _enqueue_columns(context.Session, source, template, paper_id="same-source", page_type="page_2", question="2")
    _seed_accepted_selection_candidate(context.Session, weak, tmp_path / "weak", inliers=100, ratio=0.80)
    evidence, selected, terminal, _ = business._inspect_evidence_once([weak, strong])
    assert evidence is None and selected == "" and not terminal
    _seed_accepted_selection_candidate(
        context.Session, strong, tmp_path / "strong",
        inliers=103 if ambiguous else 180, ratio=0.82 if ambiguous else 0.95,
    )
    for order in ([weak, strong], [strong, weak]):
        evidence, selected, terminal, in_progress = business._inspect_evidence_once(order)
        assert not in_progress
        if ambiguous:
            assert evidence is None and selected == "" and terminal
        else:
            assert evidence.page_type_id == "page_2" and selected == strong
            assert evidence.question_numbers == ["2"] and not terminal


def test_reference_worker_over_real_http_socket(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'e2e.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="管理员", role="admin", status="active"))
    db.commit()
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    _image(template)
    _image(source, student=True)
    layout = {
        "page_type_id": "page_1",
        "answer_regions": [{"question_number": "1", "question_type": "solve", "answer_bbox": [100, 180, 800, 420]}],
    }
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="paper-real-http",
        source_path=str(source),
        template_id="template-1",
        template_path=str(template),
        layout_snapshot=layout,
    )
    job_id = job.id
    node, token = jobs.register_node(db, owner_user_id=1, name="real-http-node")
    node_id = node.id
    db.close()

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="管理员", role="admin", status="active")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time
    for _ in range(100):
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started
    port = next(iter(server.servers)).sockets[0].getsockname()[1]
    monkeypatch.setattr(accelerator, "SessionLocal", Session, raising=False)
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_NODE_ID", node_id)
    monkeypatch.setenv("MATHAGENT_ACCELERATOR_TOKEN", token)
    args = type("Args", (), {
        "server": f"http://127.0.0.1:{port}",
        "algorithm_version": jobs.ALGORITHM_VERSION,
        "backend": "opencv_cpu_reference",
        "poll_seconds": 0.01,
        "once": True,
    })()
    try:
        assert accelerator_client.run(args) == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    verify = Session()
    stored = verify.query(AcceleratorJob).filter_by(id=job_id).one()
    assert stored.status == "validated"
    assert stored.server_validation_status == "accepted"
    assert stored.server_artifact_path
    Path(stored.server_artifact_path).unlink(missing_ok=True)
    candidate = json.loads(stored.candidate_json)
    assert candidate["worker_metrics"]["backend"] == "opencv_cpu_reference"
    assert candidate["artifact_manifest"]["contract_version"] == "client-evidence-v1"
    assert set(candidate["artifact_manifest"]) >= {"evidence"}
    validation = json.loads(stored.server_validation_json)
    assert validation["validator"] == "trusted_client_evidence_v1"
    assert validation["server_source_transform_replayed"] is False
    assert validation["server_evidence_rendered"] is False
    verify.close()


@pytest.mark.skipif(
    os.environ.get("MATHAGENT_RUN_REAL_CVCUDA") != "1",
    reason="requires the qualified WSL CV-CUDA runtime",
)
def test_wsl_cvcuda_worker_over_real_http_socket(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wsl-cvcuda-e2e.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="管理员", role="admin", status="active"))
    db.commit()
    source = Path(".cache/VulkanSift/capacity-student.jpg").resolve()
    template = Path(".cache/VulkanSift/capacity-template.jpg").resolve()
    assert source.is_file() and template.is_file()
    layout = {
        "page_type_id": "page_1",
        "answer_regions": [{
            "question_number": "1",
            "question_type": "solve",
            "answer_bbox": [300, 300, 700, 700],
        }],
    }
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="paper-wsl-cvcuda-real-http",
        source_path=str(source),
        template_id="template-wsl-cvcuda",
        template_path=str(template),
        layout_snapshot=layout,
    )
    job_id = job.id
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="wsl-cvcuda-real-http-node",
    )
    node_id = node.id
    db.close()

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(accelerator.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=1,
        name="管理员",
        role="admin",
        status="active",
    )
    route = subprocess.run(
        ["wsl.exe", "-e", "bash", "-lc", "ip route show default"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    ).stdout
    assert "default via " in route
    wsl_gateway = route.split("default via ", 1)[1].split()[0]
    server = uvicorn.Server(
        uvicorn.Config(app, host=wsl_gateway, port=0, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time
    for _ in range(100):
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started
    port = next(iter(server.servers)).sockets[0].getsockname()[1]
    monkeypatch.setattr(accelerator, "SessionLocal", Session, raising=False)
    script = (
        "cd /mnt/d/MathAgent && "
        "export PYTHONPATH=/mnt/d/MathAgent:/opt/mathagent-gpu-worker && "
        f"export MATHAGENT_ACCELERATOR_NODE_ID='{node_id}' && "
        f"export MATHAGENT_ACCELERATOR_TOKEN='{token}' && "
        "/opt/mathagent-gpu-worker/venv/bin/python "
        "-m worker.cvcuda_batch_client "
        f"--server http://{wsl_gateway}:{port} --once"
    )
    try:
        completed = subprocess.run(
            ["wsl.exe", "-e", "bash", "-lc", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        assert completed.returncode == 0, {
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    verify = Session()
    stored = verify.query(AcceleratorJob).filter_by(id=job_id).one()
    assert stored.status == "validated"
    assert stored.server_validation_status == "accepted"
    result = json.loads(stored.candidate_json)
    assert result["worker_metrics"]["backend"] == "cvcuda_single_context_v2"
    evidence_path = Path(stored.server_artifact_path)
    assert evidence_path.is_file()
    evidence_path.unlink(missing_ok=True)
    verify.close()
