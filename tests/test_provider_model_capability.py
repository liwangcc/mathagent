"""Exact profile-to-model capability contract tests for platform vision work."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_user import User
from services import accelerator_jobs as jobs


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-model-capability.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="管理员", role="admin", status="active"))
    db.commit()
    db.close()
    return Session


def _vision_job(db, tmp_path, *, paper_id: str, model: str):
    root = tmp_path / f".vision-model-{uuid.uuid4().hex}"
    root.mkdir()
    source = root / "input_01.bin"
    template = root / "input_02.bin"
    source.write_bytes(f"source-{paper_id}".encode())
    template.write_bytes(f"template-{paper_id}".encode())

    def asset(path, kind):
        data = path.read_bytes()
        return {
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "media_type": "image/png",
        }

    snapshot = {
        "contract_version": jobs.VISION_MODEL_REQUEST_CONTRACT_VERSION,
        "provider_profile": "primary",
        "model": model,
        "prompt": "只输出结果",
        "max_output_tokens": 25_000,
        "reasoning_effort": "high",
        "stream": True,
        "first_event_timeout_s": 30.0,
        "attempt_ttl_s": 60.0,
        "sse_idle_timeout_s": 30.0,
        "assets": [asset(source, "input_01"), asset(template, "input_02")],
    }
    snapshot["request_digest"] = jobs.sha256_json(snapshot)
    marker = root / ".vision-model-assets-v1"
    marker.write_text(snapshot["request_digest"], encoding="ascii")
    return jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=paper_id,
        source_path=str(source),
        template_id="primary",
        template_path=str(template),
        layout_snapshot=snapshot,
        stage=jobs.VISION_MODEL_STAGE,
        deadline_at=jobs.utcnow() + timedelta(minutes=5),
    )


def _vision_capabilities(model: str):
    return {
        "backend": "provider_api",
        "stages": [jobs.VISION_MODEL_STAGE],
        "artifact_contracts": [jobs.VISION_MODEL_RESPONSE_CONTRACT_VERSION],
        "provider_profiles": ["primary"],
        "provider_models": {"primary": model},
        "stage_slots": {
            "provider_api": {
                "stages": [jobs.VISION_MODEL_STAGE],
                "capacity": 2,
            }
        },
    }


def test_provider_model_mapping_is_normalized_and_exposed_without_credentials(
    session_factory,
):
    db = session_factory()
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="provider-model-node",
        access_scope="platform",
    )
    authenticated = jobs.authenticate_node(db, node.id, token)
    payload = jobs.heartbeat(
        db,
        authenticated,
        capabilities=_vision_capabilities("gpt-5.6-sol"),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=2,
    )

    assert payload["capabilities"]["provider_models"] == {
        "primary": "gpt-5.6-sol",
    }
    assert "apiKey" not in json.dumps(payload)
    assert jobs.node_supported_provider_models(authenticated) == {
        "primary": "gpt-5.6-sol",
    }
    db.close()


def test_vision_lease_matches_model_not_only_profile(session_factory, tmp_path):
    db = session_factory()
    older_wrong_model = _vision_job(
        db,
        tmp_path,
        paper_id="wrong-model",
        model="gpt-5.5",
    )
    matching_model = _vision_job(
        db,
        tmp_path,
        paper_id="matching-model",
        model="gpt-5.6-sol",
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="exact-model-node",
        access_scope="platform",
    )
    authenticated = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        authenticated,
        capabilities=_vision_capabilities("gpt-5.6-sol"),
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    leased, _secret = jobs.lease_next_job(
        db,
        authenticated,
        requested_stages=[jobs.VISION_MODEL_STAGE],
    )

    assert leased.id == matching_model.id
    db.refresh(older_wrong_model)
    assert older_wrong_model.status == "queued"
    db.close()


def test_legacy_profile_only_heartbeat_cannot_lease_vision_job(session_factory, tmp_path):
    db = session_factory()
    job = _vision_job(
        db,
        tmp_path,
        paper_id="legacy-profile-only",
        model="gpt-5.6-sol",
    )
    node, token = jobs.register_node(
        db,
        owner_user_id=1,
        name="legacy-node",
        access_scope="platform",
    )
    authenticated = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        authenticated,
        capabilities={
            "stages": [jobs.VISION_MODEL_STAGE],
            "artifact_contracts": [jobs.VISION_MODEL_RESPONSE_CONTRACT_VERSION],
            "provider_profiles": ["primary"],
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )

    assert jobs.lease_next_job(
        db,
        authenticated,
        requested_stages=[jobs.VISION_MODEL_STAGE],
    ) is None
    db.refresh(job)
    assert job.status == "queued"
    db.close()


def test_heartbeat_rejects_provider_model_profile_mismatch(session_factory):
    db = session_factory()
    node, token = jobs.register_node(db, owner_user_id=1, name="bad-mapping")
    authenticated = jobs.authenticate_node(db, node.id, token)

    with pytest.raises(ValueError, match="provider models"):
        jobs.heartbeat(
            db,
            authenticated,
            capabilities={
                "provider_profiles": ["primary"],
                "provider_models": {"retry": "gpt-5.6-sol"},
            },
            algorithm_version=jobs.ALGORITHM_VERSION,
            free_slots=1,
        )
    db.close()
