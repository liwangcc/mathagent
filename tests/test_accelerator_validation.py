"""Tests for lightweight validation of accelerator-produced final artifacts."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_user import User
from services import accelerator_artifacts as artifacts
from services import accelerator_jobs as jobs
from services import accelerator_validation as validation


def _image(path: Path) -> None:
    Image.new("RGB", (64, 64), "white").save(path)


def _structured_page(path: Path, *, student: bool = False) -> None:
    """Create a registration-stable page with an isolated student stroke."""
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(80, 920, 80):
        draw.line((60, y, 740, y), fill="black", width=3)
    for x in range(80, 760, 80):
        draw.rectangle((x, 40, x + 25, 65), outline="black", width=2)
    if student:
        draw.line((150, 260, 330, 320), fill="black", width=8)
        draw.line((560, 620, 710, 570), fill="black", width=8)
    image.save(path)


def _identity() -> list[list[float]]:
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _layout() -> dict:
    return {
        "page_type_id": "page_1",
        "answer_regions": [{
            "question_number": "1",
            "question_type": "solve",
            "answer_bbox": [0, 0, 64, 64],
        }],
    }


def _database(tmp_path: Path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="admin", role="admin", status="active"))
    db.commit()
    return db


def _enqueue_and_lease(
    db,
    tmp_path: Path,
    paper_id: str,
    *,
    backend: str = "cvcuda_single_context_v2",
):
    source = tmp_path / f"{paper_id}-source.png"
    template = tmp_path / f"{paper_id}-template.png"
    _image(source)
    _image(template)
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=paper_id,
        source_path=str(source),
        template_id=f"template-{paper_id}",
        template_path=str(template),
        layout_snapshot=_layout(),
    )
    node, token = jobs.register_node(db, owner_user_id=1, name="node")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": ["template_registration"],
            "artifact_contracts": ["client-evidence-v1"],
            "backend": backend,
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, lease = jobs.lease_next_job(db, node)
    assert leased.id == job.id
    return job, node, lease, source, template


def _enqueue_multi_and_lease(db, tmp_path: Path, paper_id: str):
    from services.template_anchor_crops import build_anchor_crop_plan

    source = tmp_path / f"{paper_id}-source.png"
    template = tmp_path / f"{paper_id}-template.png"
    _image(source)
    _image(template)
    template_sha = artifacts.sha256_file(template)
    layout = _layout()
    layout["answer_regions"] = [
        {
            "question_number": "1",
            "question_type": "solve",
            "answer_bbox": [0, 0, 330, 1000],
        },
        {
            "question_number": "2",
            "question_type": "solve",
            "answer_bbox": [330, 0, 665, 1000],
        },
        {
            "question_number": "3",
            "question_type": "solve",
            "answer_bbox": [665, 0, 1000, 1000],
        },
    ]
    layout["question_crop_plan"] = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
                    {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
                    {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
                ],
            }
        },
        artifact_sha256=template_sha,
        expected_question_numbers=["1", "2", "3"],
    )
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=paper_id,
        source_path=str(source),
        template_id=f"template-{paper_id}",
        template_path=str(template),
        layout_snapshot=layout,
        stage=jobs.STUDENT_MULTI_EVIDENCE_STAGE,
    )
    node, token = jobs.register_node(db, owner_user_id=1, name=f"node-{paper_id}")
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.STUDENT_MULTI_EVIDENCE_STAGE],
            "artifact_contracts": [artifacts.CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION],
            "backend": "cvcuda_single_context_v2",
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, lease = jobs.lease_next_job(db, node)
    assert leased.id == job.id
    return job, node, lease, source, template, layout


def _enqueue_full_column_and_lease(
    db, tmp_path: Path, paper_id: str, *, bound: bool = False, platform: bool = False
):
    from services.template_anchor_crops import build_anchor_crop_plan

    source = tmp_path / f"{paper_id}-source.png"
    template = tmp_path / f"{paper_id}-template.png"
    _structured_page(template)
    _structured_page(source, student=True)
    template_sha = artifacts.sha256_file(template)
    question_numbers = ["1", "2"]
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            }
        },
        artifact_sha256=template_sha,
        expected_question_numbers=question_numbers,
    )
    layout = {
        "page_type_id": "page_1",
        "question_schema": [
            {
                "question_number": number,
                "question_type": "solve",
                "choice_mode": "unknown",
            }
            for number in question_numbers
        ],
        "question_numbers": question_numbers,
        "blank_detection_enabled": False,
        "question_anchors": [
            {"question_number": "1", "question_number_bbox": [100, 100, 140, 125]},
            {"question_number": "2", "question_number_bbox": [600, 500, 640, 525]},
        ],
        "question_crop_plan": plan,
    }
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id=paper_id,
        source_path=str(source),
        template_id=f"template-{paper_id}",
        template_path=str(template),
        layout_snapshot=layout,
        stage=jobs.STUDENT_MULTI_EVIDENCE_STAGE,
    )
    assert not (bound and platform)
    if platform:
        db.add(User(id=2, name="platform-admin", role="admin", status="active"))
        db.commit()
    node, token = jobs.register_node(
        db, owner_user_id=2 if platform else 1, name=f"node-{paper_id}",
        access_scope="platform" if platform else "tenant",
        lane="teacher_desktop" if bound else "web_gpu",
        device_id=f"device-{paper_id}" if bound else None,
    )
    node = jobs.authenticate_node(db, node.id, token)
    jobs.heartbeat(
        db,
        node,
        capabilities={
            "stages": [jobs.STUDENT_MULTI_EVIDENCE_STAGE],
            "artifact_contracts": [artifacts.CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION],
            "backend": "cvcuda_single_context_v2",
        },
        algorithm_version=jobs.ALGORITHM_VERSION,
        free_slots=1,
    )
    leased, lease = jobs.lease_next_job(db, node)
    assert leased.id == job.id
    return job, node, lease, source, template, layout


def _legacy_crop_plan(source_sha256: str) -> dict:
    return {
        "version": 1,
        "strategy": "top_anchor_continuous_half_columns",
        "coordinate_space": "template_1000",
        "source_artifact_sha256": source_sha256,
        "provider_bottom_ignored": True,
        "requires_question_number_gate": True,
        "parameters": {
            "split_x_1000": 500,
            "column_overlap_1000": 12,
            "vertical_overlap_1000": 10,
            "anchors_per_block": 3,
            "anchor_merge_tolerance_1000": 8,
        },
        "expected_question_numbers": ["1"],
        "anchors": [],
        "blocks": [
            {
                "block_id": "left-1",
                "column": "left",
                "bbox_1000": [0, 0, 512, 1000],
                "source_anchor_ids": [],
                "question_number_hints": [],
                "fallback_full_column": True,
            },
            {
                "block_id": "right-1",
                "column": "right",
                "bbox_1000": [488, 0, 1000, 1000],
                "source_anchor_ids": [],
                "question_number_hints": [],
                "fallback_full_column": True,
            },
        ],
        "warnings": ["no_trusted_anchors:left", "no_trusted_anchors:right"],
        "source_summary": {
            "trusted_candidate_count": 0,
            "rejected_candidate_count": 0,
        },
    }


def test_accelerator_crop_plan_rejects_removed_top_anchor_contract():
    source_sha256 = "a" * 64
    with pytest.raises(ValueError, match="unsupported version"):
        validation._canonical_crop_plan(
            _legacy_crop_plan(source_sha256),
            artifact_sha256=source_sha256,
        )


def test_accelerator_crop_plan_comparison_rejects_noncanonical_v2():
    from services.template_anchor_crops import build_anchor_crop_plan

    source_sha256 = "a" * 64
    plan = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                ],
            }
        },
        artifact_sha256=source_sha256,
    )
    plan["unexpected"] = True

    with pytest.raises(ValueError, match="not canonical"):
        validation._canonical_crop_plan(
            plan,
            artifact_sha256=source_sha256,
        )


def _multi_evidence_candidate(
    job,
    template,
    layout,
    tmp_path: Path,
    *,
    wrong_metadata: bool = False,
    one_image: bool = False,
):
    aligned = artifacts.materialize_global_artifacts(
        template,
        template,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        tmp_path / f"{job.id}-aligned",
    )
    sheets = validation.build_focus_evidence_sheet_layout(
        layout["answer_regions"],
        64,
        64,
        layout["question_crop_plan"],
    )
    paths = []
    metadata = []
    for index, sheet in enumerate(sheets[:1] if one_image else sheets, 1):
        path = tmp_path / f"{job.id}-crop-{index}.png"
        Image.new("RGB", (sheet["width"], sheet["height"]), "white").save(path)
        paths.append(path)
        regions = [dict(region) for region in sheet["regions"]]
        if wrong_metadata and index == 1:
            regions[0]["question_number"] = "unexpected"
        metadata.append({
            "sheet_id": sheet["sheet_id"],
            "regions": regions,
        })
    manifest = artifacts.client_multi_evidence_manifest(
        aligned.manifest,
        paths,
        metadata,
    )
    root = artifacts.candidate_artifact_root(job.source_path, job.id)
    root.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(paths, 1):
        (root / f"evidence_{index:02d}.png").write_bytes(path.read_bytes())
    candidate = {
        "status": "succeeded",
        "page_type_id": "page_1",
        "homography": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        "good_matches": 100,
        "inliers": 90,
        "inlier_ratio": 0.9,
        "reprojection_rmse": 1.0,
        "coverage": 1.0,
        "answer_source_complete": True,
        "incomplete_answer_regions": [],
        "artifact_manifest": manifest,
        "question_numbers": ["1", "2", "3"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {
            "ownership": "confirmed_bbox_focus_sheets",
            "answer_source_coverage": [
                {
                    "question_number": region["question_number"],
                    "covered_pixels": 4096,
                    "total_pixels": 4096,
                    "coverage": 1.0,
                    "complete": True,
                    "crop_box": [0, 0, 64, 64],
                }
                for region in layout["answer_regions"]
            ],
            "incomplete_answer_regions": [],
            "evidence_sheets": metadata,
        },
    }
    candidate["artifact_sha256"] = artifacts.candidate_artifact_sha256(
        page_type_id="page_1",
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    return candidate


def _full_column_evidence_candidate(job, template, layout, tmp_path: Path):
    aligned = artifacts.materialize_global_artifacts(
        job.source_path,
        template,
        _identity(),
        tmp_path / f"{job.id}-worker-transform",
    )
    paths = []
    columns = []
    for index, question_numbers in enumerate((["1"], ["2"]), 1):
        path = tmp_path / f"{job.id}-column-{index}.png"
        Image.new("RGB", (400, 1000), "white").save(path)
        paths.append(path)
        columns.append({
            "sheet_id": f"column_{index:02d}",
            "question_numbers": question_numbers,
        })
    manifest = artifacts.client_full_column_evidence_manifest(
        aligned.manifest,
        paths,
        columns,
    )
    root = artifacts.candidate_artifact_root(job.source_path, job.id)
    root.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(paths, 1):
        (root / f"evidence_{index:02d}.png").write_bytes(path.read_bytes())
    candidate = {
        "status": "succeeded",
        "page_type_id": "page_1",
        "homography": _identity(),
        "good_matches": 100,
        "inliers": 90,
        "inlier_ratio": 0.9,
        "reprojection_rmse": 1.0,
        "coverage": 1.0,
        "answer_source_complete": True,
        "incomplete_answer_regions": [],
        "artifact_manifest": manifest,
        "question_numbers": ["1", "2"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {
            "ownership": "full_column_difference",
            "evidence_sheets": columns,
        },
    }
    candidate["artifact_sha256"] = artifacts.candidate_artifact_sha256(
        page_type_id="page_1",
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    return candidate, paths


def test_multi_evidence_validation_is_decode_free_ordered_and_cleaned(
    tmp_path,
    monkeypatch,
):
    from services import accelerator_business as business

    db = _database(tmp_path, "multi-evidence.db")
    job, node, lease, _source, template, layout = _enqueue_multi_and_lease(
        db,
        tmp_path,
        "paper-multi-evidence",
    )
    candidate = _multi_evidence_candidate(job, template, layout, tmp_path)
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("multi-evidence validation decoded image pixels")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)

    validated = validation.validate_candidate_job(db, job.id)

    assert validated.status == "validated"
    details = json.loads(validated.server_validation_json)
    assert details["validator"] == "trusted_client_evidence_multi_v3"
    assert details["server_image_decode_performed"] is False
    manifest_path = Path(validated.server_artifact_path)
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["kind"] for item in persisted["evidence_images"]] == [
        "evidence_01",
        "evidence_02",
        "evidence_03",
    ]
    expected_sheets = validation.build_focus_evidence_sheet_layout(
        layout["answer_regions"],
        64,
        64,
        layout["question_crop_plan"],
    )
    assert [item["width"] for item in persisted["evidence_images"]] == [
        item["width"] for item in expected_sheets
    ]
    consumed = business._validated_evidence(validated)
    assert consumed is not None
    assert [Path(path).name for path in consumed.evidence_paths] == [
        "evidence_01.png",
        "evidence_02.png",
        "evidence_03.png",
    ]

    monkeypatch.setattr(
        business,
        "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False),
    )
    assert business.finish_business_preprocessing(
        business.AcceleratorEvidence(
            consumed,
            (job.id,),
            job.id,
            state="ready",
        ),
        fallback_succeeded=False,
    ) == 1
    assert not manifest_path.parent.exists()
    db.close()


def test_full_column_validation_replays_source_and_discards_client_pixels(tmp_path):
    from services import accelerator_business as business

    db = _database(tmp_path, "full-column-replay.db")
    job, node, lease, source, template, layout = _enqueue_full_column_and_lease(
        db,
        tmp_path,
        "paper-full-column-replay",
    )
    candidate, client_paths = _full_column_evidence_candidate(
        job,
        template,
        layout,
        tmp_path,
    )
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )

    validated = validation.validate_candidate_job(db, job.id)

    assert validated.status == "validated"
    details = json.loads(validated.server_validation_json)
    assert details["validator"] == artifacts.SERVER_REPLAYED_FULL_COLUMN_VALIDATOR
    assert details["server_image_decode_performed"] is True
    assert details["server_source_transform_replayed"] is True
    assert details["server_evidence_rendered"] is True
    assert details["client_evidence_pixels_trusted"] is False

    manifest_path = Path(validated.server_artifact_path)
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    persisted_paths = [manifest_path.parent / f"evidence_{index:02d}.png" for index in (1, 2)]
    assert [item["kind"] for item in persisted["evidence_images"]] == [
        "evidence_01",
        "evidence_02",
    ]
    assert all(path.is_file() for path in persisted_paths)

    # The submitted sidecars are deliberately all white.  The published left
    # column must nevertheless contain the student's stroke regenerated from
    # the immutable source page.
    with Image.open(client_paths[0]) as client_image:
        assert all(pixel == (255, 255, 255) for pixel in client_image.getdata())
    with Image.open(persisted_paths[0]) as rendered_image:
        stroke_crop = rendered_image.crop((80, 250, 380, 330)).convert("RGB")
        assert sum(sum(pixel) < 600 for pixel in stroke_crop.getdata()) > 20

    consumed = business._validated_evidence(validated)
    assert consumed is not None
    assert consumed.diagnostics["accelerator"]["evidence_authority"] == (
        "server_rendered_full_columns"
    )
    assert consumed.diagnostics["accelerator"]["server_source_transform_replayed"] is True
    assert [Path(path).name for path in consumed.evidence_paths] == [
        "evidence_01.png",
        "evidence_02.png",
    ]
    assert source.is_file() and template.is_file()
    db.close()


def _bound_full_column_candidate(job, template, layout, tmp_path):
    """Produce real local difference columns and the local replay proof."""
    candidate, _ = _full_column_evidence_candidate(job, template, layout, tmp_path)
    transformed = artifacts.materialize_global_artifacts(
        job.source_path, template, candidate["homography"], tmp_path / "bound-transform"
    )
    replay = artifacts.replay_and_verify_artifacts(
        source_path=job.source_path,
        template_path=str(template),
        homography=candidate["homography"],
        artifact_manifest=transformed.manifest,
        aligned_page_path=transformed.aligned_page_path,
        source_mask_path=transformed.source_mask_path,
    )
    rendered = validation.build_full_column_difference_evidence_from_aligned(
        replay.aligned_rgb, replay.source_mask, replay.template_rgb, layout,
        tmp_path / "bound-evidence",
    )
    paths = [Path(path) for path in rendered.evidence_paths]
    columns = [
        {"sheet_id": item["sheet_id"], "question_numbers": item["question_numbers"]}
        for item in rendered.diagnostics["evidence_columns"]
    ]
    manifest = artifacts.client_full_column_evidence_manifest(
        transformed.manifest, paths, columns
    )
    for image, path in zip(manifest["evidence_images"], paths):
        artifacts.candidate_artifact_path(
            job.source_path, job.id, image["kind"]
        ).write_bytes(path.read_bytes())
    candidate["artifact_manifest"] = manifest
    candidate["evidence_diagnostics"]["evidence_sheets"] = columns
    candidate["artifact_sha256"] = artifacts.candidate_artifact_sha256(
        page_type_id=candidate["page_type_id"],
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=candidate["homography"],
        artifact_manifest=manifest,
    )
    candidate["local_quality_report"] = {
        "version": artifacts.LOCAL_FULL_COLUMN_CHECK_VERSION,
        "source_sha256": job.source_sha256,
        "template_sha256": job.template_sha256,
        "layout_snapshot_sha256": job.layout_snapshot_sha256,
        "artifact_sha256": candidate["artifact_sha256"],
        "page_type_id": candidate["page_type_id"],
        "metrics": {
            "coverage": replay.coverage,
            "aligned_replay_mae": replay.aligned_mae,
            "aligned_replay_p99": replay.aligned_p99,
            "template_edge_recall": replay.template_edge_recall,
            "gradient_cosine": replay.gradient_cosine,
            "gradient_x_cosine": replay.gradient_x_cosine,
            "gradient_y_cosine": replay.gradient_y_cosine,
            "template_edge_pixels": replay.template_edge_pixels,
            "aligned_edge_pixels": replay.aligned_edge_pixels,
        },
    }
    return candidate, paths


@pytest.mark.parametrize("identity", ["teacher", "platform"])
def test_bound_full_column_validation_preserves_exact_local_pixels_without_replay(
    tmp_path, monkeypatch, identity
):
    from services import accelerator_business as business

    db = _database(tmp_path, "bound-full-column.db")
    job, node, lease, source, template, layout = _enqueue_full_column_and_lease(
        db, tmp_path, "bound-column", bound=identity == "teacher",
        platform=identity == "platform",
    )
    candidate, paths = _bound_full_column_candidate(job, template, layout, tmp_path)
    jobs.submit_result(db, node, job_id=job.id, lease_secret=lease, result=candidate)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("bound full-column validation performed server pixel work")

    for name in (
        "materialize_global_artifacts", "replay_and_verify_artifacts",
        "build_full_column_difference_evidence_from_aligned",
    ):
        monkeypatch.setattr(validation, name, forbidden)
    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)

    validated = validation.validate_candidate_job(db, job.id)

    assert validated.status == "validated", validated.server_validation_json
    details = json.loads(validated.server_validation_json)
    assert details["validator"] == artifacts.BOUND_CLIENT_FULL_COLUMN_VALIDATOR
    assert details["bound_node_identity_validated"] is True
    assert details["node_access_scope"] == ("platform" if identity == "platform" else "tenant")
    assert details["local_quality_report_validated"] is True
    assert details["server_image_decode_performed"] is False
    assert details["server_source_transform_replayed"] is False
    assert details["server_evidence_rendered"] is False
    consumed = business._validated_evidence(validated)
    assert consumed is not None
    assert consumed.diagnostics["accelerator"]["evidence_authority"] == (
        "bound_client_full_columns"
    )
    assert [Path(path).read_bytes() for path in consumed.evidence_paths] == [
        path.read_bytes() for path in paths
    ]
    assert not artifacts.candidate_artifact_root(source, job.id).exists()
    assert source.is_file() and template.is_file()
    db.close()


@pytest.mark.parametrize("failure", [
    "disabled", "revoked", "expired", "tenant_without_binding", "unknown_scope", "job_lane",
])
def test_platform_full_column_report_respects_current_node_authorization(
    tmp_path, monkeypatch, failure
):
    from datetime import timedelta

    db = _database(tmp_path, "platform-authorization.db")
    job, node, lease, _source, template, layout = _enqueue_full_column_and_lease(
        db, tmp_path, "platform-authorization", platform=True
    )
    candidate, _ = _bound_full_column_candidate(job, template, layout, tmp_path)
    jobs.submit_result(db, node, job_id=job.id, lease_secret=lease, result=candidate)
    if failure == "disabled":
        node.enabled = 0
    elif failure == "revoked":
        node.revoked_at = jobs.utcnow()
    elif failure == "expired":
        node.token_expires_at = jobs.utcnow() - timedelta(seconds=1)
    elif failure == "tenant_without_binding":
        node.access_scope = "tenant"
        node.owner_user_id = job.teacher_id
    elif failure == "unknown_scope":
        node.access_scope = "unknown"
    elif failure == "job_lane":
        job.lane = "unknown"
    db.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unauthorized platform report triggered server replay")

    monkeypatch.setattr(validation, "materialize_global_artifacts", forbidden)
    result = validation.validate_candidate_job(db, job.id)
    assert result.status == "fallback_failed", result.server_validation_json
    assert result.server_validation_status == "rejected"
    db.close()


@pytest.mark.parametrize("failure", [
    "source", "template", "layout", "page_type", "artifact", "question_order",
    "version", "quality", "incomplete", "unbound", "cross_teacher", "revoked",
    "missing_file", "tampered_file",
])
def test_bound_full_column_rejects_mismatched_identity_or_quality_without_replay(
    tmp_path, monkeypatch, failure
):
    db = _database(tmp_path, "bound-reject.db")
    job, node, lease, _source, template, layout = _enqueue_full_column_and_lease(
        db, tmp_path, f"bound-{failure}", bound=True
    )
    candidate, _ = _bound_full_column_candidate(job, template, layout, tmp_path)
    jobs.submit_result(db, node, job_id=job.id, lease_secret=lease, result=candidate)
    report = candidate["local_quality_report"]
    if failure in {"source", "template", "layout", "artifact"}:
        key = "layout_snapshot_sha256" if failure == "layout" else f"{failure}_sha256"
        report[key] = "0" * 64
    elif failure == "page_type":
        report["page_type_id"] = "page_2"
    elif failure == "question_order":
        candidate["question_numbers"].reverse()
    elif failure == "version":
        report["version"] = "historical-unchecked-columns"
    elif failure == "quality":
        report["metrics"]["template_edge_recall"] = 0.01
    elif failure == "incomplete":
        candidate["answer_source_complete"] = False
        candidate["incomplete_answer_regions"] = ["1"]
    # Tamper after the normal authenticated lease submission so the acceptance
    # layer must independently respect revocation/ownership and file identity.
    if failure == "unbound":
        node.device_id = None
    elif failure == "cross_teacher":
        node.owner_user_id = 2
    elif failure == "revoked":
        node.enabled = 0
    elif failure == "missing_file":
        artifacts.candidate_artifact_path(
            job.source_path, job.id, "evidence_01"
        ).unlink()
    elif failure == "tampered_file":
        artifacts.candidate_artifact_path(
            job.source_path, job.id, "evidence_01"
        ).write_bytes(b"not the submitted column")
    job.candidate_json = json.dumps(candidate)
    db.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid local quality report silently used server replay")

    monkeypatch.setattr(validation, "materialize_global_artifacts", forbidden)
    validated = validation.validate_candidate_job(db, job.id)
    assert validated.status == "fallback_failed", validated.server_validation_json
    details = json.loads(validated.server_validation_json)
    assert details["validator"] == artifacts.BOUND_CLIENT_FULL_COLUMN_VALIDATOR
    assert details["reason"] == "client_evidence_contract_rejected"
    db.close()


@pytest.mark.parametrize("failure", ["metadata", "missing", "tampered", "count"])
def test_multi_evidence_validation_fails_closed_on_inconsistent_set(
    tmp_path,
    failure,
):
    db = _database(tmp_path, f"multi-{failure}.db")
    job, node, lease, _source, template, layout = _enqueue_multi_and_lease(
        db,
        tmp_path,
        f"paper-multi-{failure}",
    )
    candidate = _multi_evidence_candidate(
        job,
        template,
        layout,
        tmp_path,
        wrong_metadata=failure == "metadata",
        one_image=failure == "count",
    )
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )
    second = artifacts.candidate_artifact_path(
        job.source_path,
        job.id,
        "evidence_02",
    )
    if failure == "missing":
        second.unlink()
    elif failure == "tampered":
        second.write_bytes(b"changed")

    rejected = validation.validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    assert rejected.server_validation_status == "rejected"
    details = json.loads(rejected.server_validation_json)
    assert details["validator"] == "trusted_client_evidence_multi_v3"
    assert details["server_image_decode_performed"] is False
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    assert list(tmp_path.glob("evidence-set-*")) == []
    db.close()


def _evidence_candidate(
    job,
    *,
    good_matches: int = 100,
    inliers: int = 90,
) -> tuple[dict, Path, bytes]:
    root = artifacts.candidate_artifact_root(job.source_path, job.id)
    root.mkdir(parents=True, exist_ok=True)
    evidence_path = root / "evidence.png"
    Image.new("RGB", (120, 40), "white").save(evidence_path)
    evidence_bytes = evidence_path.read_bytes()
    evidence_file = artifacts.validate_uploaded_artifact_file(
        evidence_path,
        "evidence",
    )
    manifest = artifacts.validate_artifact_manifest({
        "contract_version": artifacts.CLIENT_EVIDENCE_CONTRACT_VERSION,
        "coordinate_space": artifacts.CLIENT_EVIDENCE_COORDINATE_SPACE,
        "transform_graph": artifacts.TRANSFORM_GRAPH,
        "renderer": artifacts.CLIENT_EVIDENCE_RENDERER,
        "source_width": 64,
        "source_height": 64,
        "template_width": 64,
        "template_height": 64,
        "evidence": evidence_file,
    })
    homography = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    candidate = {
        "status": "succeeded",
        "page_type_id": "page_1",
        "homography": homography,
        "good_matches": good_matches,
        "inliers": inliers,
        "inlier_ratio": inliers / good_matches,
        "reprojection_rmse": 1.0,
        "coverage": 1.0,
        "answer_source_complete": True,
        "incomplete_answer_regions": [],
        "artifact_manifest": manifest,
        "question_numbers": ["1"],
        "blank_question_numbers": [],
        "evidence_diagnostics": {
            "ownership": "confirmed_bbox_neighbor_clamped",
            "answer_source_coverage": [{
                "question_number": "1",
                "covered_pixels": 4096,
                "total_pixels": 4096,
                "coverage": 1.0,
                "complete": True,
                "crop_box": [0, 0, 64, 64],
            }],
            "incomplete_answer_regions": [],
        },
    }
    candidate["artifact_sha256"] = artifacts.candidate_artifact_sha256(
        page_type_id="page_1",
        source_sha256=job.source_sha256,
        template_sha256=job.template_sha256,
        layout_snapshot_sha256=job.layout_snapshot_sha256,
        homography=homography,
        artifact_manifest=manifest,
    )
    return candidate, evidence_path, evidence_bytes


def _legacy_manifest() -> dict:
    return {
        "contract_version": "aligned-page-v1",
        "coordinate_space": "raw_source_pixel_to_template_pixel",
        "transform_graph": "global_homography_only_v1",
        "renderer": "opencv_warp_v1",
        "source_width": 64,
        "source_height": 64,
        "template_width": 64,
        "template_height": 64,
        "aligned_page": {
            "sha256": "a" * 64,
            "byte_size": 100,
            "width": 64,
            "height": 64,
            "channels": 3,
            "format": "png",
        },
        "source_mask": {
            "sha256": "b" * 64,
            "byte_size": 100,
            "width": 64,
            "height": 64,
            "channels": 1,
            "format": "png",
        },
    }


def test_lightweight_validation_does_not_serialize_on_a_replay_slot(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()
    all_entered = threading.Event()
    release = threading.Event()
    results: list[str] = []

    def blocked_validate(_db, job_id):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 3:
                all_entered.set()
        try:
            assert release.wait(5.0)
            return job_id
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(validation, "validate_candidate_job", blocked_validate)
    threads = [
        threading.Thread(
            target=lambda value=index: results.append(
                validation.try_validate_candidate_job(None, str(value))
            )
        )
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    try:
        assert all_entered.wait(5.0)
        assert maximum == 3
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5.0)
    assert sorted(results) == ["0", "1", "2"]


def test_final_client_evidence_skips_all_server_pixel_processing(
    tmp_path,
    monkeypatch,
):
    db = _database(tmp_path, "client-evidence.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-client-evidence",
    )
    candidate, _evidence_path, expected_evidence = _evidence_candidate(job)
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server pixel processing was invoked")

    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    monkeypatch.setattr(artifacts, "replay_and_verify_artifacts", forbidden)

    validated = validation.validate_candidate_job(db, job.id)

    assert validated.status == "validated"
    details = json.loads(validated.server_validation_json)
    assert details["validator"] == "trusted_client_evidence_v1"
    assert details["server_image_decode_performed"] is False
    assert details["server_source_transform_replayed"] is False
    assert details["server_evidence_rendered"] is False
    stable = Path(validated.server_artifact_path)
    assert stable.read_bytes() == expected_evidence
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    stable.unlink()
    db.close()


def test_zero_reprojection_rmse_is_eligible_for_local_first_validation(tmp_path):
    db = _database(tmp_path, "zero-rmse.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-zero-rmse",
    )
    candidate, _evidence_path, _expected_evidence = _evidence_candidate(job)
    candidate["reprojection_rmse"] = 0.0
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )

    validated = validation.validate_candidate_job(db, job.id)

    assert validated.status == "validated"
    Path(validated.server_artifact_path).unlink(missing_ok=True)
    db.close()


def test_legacy_aligned_result_is_rejected_at_submit_before_geometry(
    tmp_path,
    monkeypatch,
):
    db = _database(tmp_path, "legacy-submit.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-legacy-submit",
    )
    monkeypatch.setattr(
        jobs,
        "normalize_homography",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy contract reached geometry validation")
        ),
    )

    with pytest.raises(ValueError, match="requires client-evidence-v1"):
        jobs.submit_result(
            db,
            node,
            job_id=job.id,
            lease_secret=lease,
            result={"artifact_manifest": _legacy_manifest()},
        )

    db.refresh(job)
    assert job.status == "leased"
    assert job.candidate_json is None
    db.close()


def test_persisted_legacy_candidate_is_rejected_before_layout_or_pixels(
    tmp_path,
    monkeypatch,
):
    db = _database(tmp_path, "legacy-persisted.db")
    job, _node, _lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-legacy-persisted",
    )
    root = artifacts.candidate_artifact_root(job.source_path, job.id)
    root.mkdir(parents=True)
    (root / "aligned_page.png").write_bytes(b"not-a-png")
    (root / "source_mask.png").write_bytes(b"not-a-png")
    job.status = "candidate_received"
    job.server_validation_status = "pending"
    job.candidate_json = json.dumps({"artifact_manifest": _legacy_manifest()})
    db.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy contract reached server image processing")

    monkeypatch.setattr(validation, "_layout", forbidden)
    monkeypatch.setattr(artifacts.cv2, "imdecode", forbidden)
    monkeypatch.setattr(artifacts.Image, "open", forbidden)
    monkeypatch.setattr(artifacts, "replay_and_verify_artifacts", forbidden)

    rejected = validation.validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    details = json.loads(rejected.server_validation_json)
    assert details["reason"] == "client_evidence_contract_required"
    assert details["server_image_decode_performed"] is False
    assert details["server_source_transform_replayed"] is False
    assert not root.exists()
    db.close()


@pytest.mark.parametrize(
    ("backend", "good_matches", "inliers"),
    [
        ("cvcuda_single_context_v2", 20, 18),
        ("unregistered_backend", 100, 90),
    ],
)
def test_unqualified_client_evidence_fails_closed_without_replay(
    tmp_path,
    monkeypatch,
    backend,
    good_matches,
    inliers,
):
    db = _database(tmp_path, f"unqualified-{good_matches}.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        f"paper-unqualified-{good_matches}",
        backend=backend,
    )
    candidate, _path, _bytes = _evidence_candidate(
        job,
        good_matches=good_matches,
        inliers=inliers,
    )
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )
    monkeypatch.setattr(
        artifacts,
        "replay_and_verify_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unqualified evidence entered server replay")
        ),
    )

    rejected = validation.validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    details = json.loads(rejected.server_validation_json)
    assert details["reason"] == "client_evidence_contract_rejected"
    assert details["server_source_transform_replayed"] is False
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    db.close()


@pytest.mark.parametrize("changed", ["source", "template", "evidence"])
def test_bound_file_changes_are_rejected(tmp_path, changed):
    db = _database(tmp_path, f"changed-{changed}.db")
    job, node, lease, source, template = _enqueue_and_lease(
        db,
        tmp_path,
        f"paper-changed-{changed}",
    )
    candidate, evidence_path, _bytes = _evidence_candidate(job)
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )
    {"source": source, "template": template, "evidence": evidence_path}[
        changed
    ].write_bytes(b"changed")

    rejected = validation.validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    assert rejected.server_validation_status == "rejected"
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    db.close()


def test_try_validate_candidate_job_fails_closed_on_unexpected_error(
    tmp_path,
    monkeypatch,
):
    db = _database(tmp_path, "internal-error.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-internal-error",
    )
    candidate, _path, _bytes = _evidence_candidate(job)
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )
    monkeypatch.setattr(
        validation,
        "_validate_client_evidence_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    rejected = validation.try_validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    assert rejected.server_validation_status == "rejected"
    details = json.loads(rejected.server_validation_json)
    assert details["reason"] == "server_validation_internal_error"
    assert details["server_image_decode_performed"] is False
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    db.close()


def test_unpersisted_server_artifact_is_removed_on_finish_error(
    tmp_path,
    monkeypatch,
):
    db = _database(tmp_path, "artifact-cleanup.db")
    job, node, lease, _source, _template = _enqueue_and_lease(
        db,
        tmp_path,
        "paper-cleanup",
    )
    candidate, _path, _bytes = _evidence_candidate(job)
    jobs.submit_result(
        db,
        node,
        job_id=job.id,
        lease_secret=lease,
        result=candidate,
    )
    original_finish = validation.finish_server_validation
    failed_once = False

    def fail_first_accept(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("accepted") and not failed_once:
            failed_once = True
            raise RuntimeError("finish failed")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(validation, "finish_server_validation", fail_first_accept)

    rejected = validation.try_validate_candidate_job(db, job.id)

    assert rejected.status == "fallback_failed"
    assert list(tmp_path.glob("evidence-*.png")) == []
    assert not artifacts.candidate_artifact_root(job.source_path, job.id).exists()
    db.close()


def test_competing_validation_request_does_not_demote_active_validator(tmp_path):
    db = _database(tmp_path, "validation-race.db")
    source = tmp_path / "race-source.png"
    template = tmp_path / "race-template.png"
    _image(source)
    _image(template)
    job = jobs.enqueue_job(
        db,
        teacher_id=1,
        paper_id="paper-race",
        source_path=str(source),
        template_id="template-race",
        template_path=str(template),
        layout_snapshot=_layout(),
    )
    job.status = "server_validating"
    job.server_validation_status = "running"
    db.commit()

    raced = validation.try_validate_candidate_job(db, job.id)

    assert raced.status == "server_validating"
    assert raced.server_validation_status == "running"
    db.close()
