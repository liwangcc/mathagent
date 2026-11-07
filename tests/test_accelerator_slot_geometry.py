"""Synthetic frozen geometry through business enqueue, DB JSON and worker render."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_accelerator  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models_accelerator import AcceleratorJob
from models_user import User
from services import accelerator_business as business
from services import accelerator_jobs as jobs
from services import template_evidence as evidence
from services.template_anchor_crops import build_anchor_crop_plan
from services import template_evidence_matcher


@pytest.fixture
def enqueue(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(User(id=1, name="synthetic teacher", role="teacher", status="active"))
        db.commit()
    monkeypatch.setattr(business, "SessionLocal", Session)

    def submit(artifact):
        result = business._enqueue_template_pages(
            teacher_id=1, batch_id="synthetic-batch", paper_id="synthetic-paper",
            source_path=str(tmp_path / "student.png"), template_id="synthetic-template",
            artifacts={"page_1": artifact}, template_dir=str(tmp_path))
        # Reopen the persisted row: do not assert on an in-memory input or a
        # helper that can heal the dropped fields on the read side.
        with Session() as db:
            row = db.get(AcceleratorJob, result.job_ids[0])
            payload = jobs.job_public(row)
            payload["layout_snapshot"] = json.loads(row.layout_snapshot_json)
            return payload

    submit.Session = Session
    yield submit
    engine.dispose()


def _frozen_artifact(tmp_path, *, mode="marked", kind="solve", columns=1, prewhitened=False):
    # Printed features are identical; student-only strokes are synthetic.
    template = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(template)
    for x in range(30, 1170, 12):
        draw.rectangle((x, 12, x + 5, 55), outline="black", width=2)
    if prewhitened:
        draw.line((350, 95, 430, 115), fill="black", width=4)
    original = tmp_path / "printed.png"
    template.save(original)
    boxes = [[x, 100 + row * 200, x + 160, 180 + row * 200]
             for row in range(4) for x in (250, 710)]
    # Deliberately non-monotone and sparse: sorting/reindexing is forbidden.
    ids = ["2[3]", "2[1]", "2[5]", "2[7]", "2[8]", "2[9]", "2[11]", "2[12]"]
    regions = [{"question_number": "2", "regions": boxes, "slot_ids": ids,
                "region_kinds": ["answer"] * len(ids)}]
    schema = [{"question_number": "2", "question_type": kind, "slot_ids": ids,
               "region_kinds": ["answer"] * len(ids)}]
    if columns == 2:
        # The existing worker manifest requires disjoint question ownership
        # between columns. Keep that contract while exercising both columns.
        regions = [
            {"question_number": number, "regions": boxes[side::2],
             "slot_ids": [f"{number}[{slot}]" for slot in (3, 1, 8, 12)],
             "region_kinds": ["answer"] * 4}
            for side, number in enumerate(("2", "4"))]
        schema = [{"question_number": item["question_number"], "question_type": kind,
                   "slot_ids": item["slot_ids"], "region_kinds": item["region_kinds"]}
                  for item in regions]
    artifact = evidence.build_template_artifact(
        str(original), str(tmp_path), 0, schema, template_mode=mode,
        blank_detection_enabled=False, template_answer_regions=regions)
    # Exemptions are not answer ownership. In particular they may be empty.
    artifact["template_handwriting_regions"] = []
    if prewhitened:
        artifact["template_handwriting_regions"] = copy.deepcopy(regions)
        artifact = evidence.materialize_template_difference_artifact(tmp_path, artifact)
    artifact["question_crop_plan"] = build_anchor_crop_plan(
        {"column_layout": {"column_count": columns, "columns": [
            {"column_index": i + 1, "x1_1000": round(i * 1000 / columns),
             "x2_1000": round((i + 1) * 1000 / columns)} for i in range(columns)]}},
        artifact_sha256=artifact["sha256"], expected_question_numbers=[
            item["question_number"] for item in schema
        ])
    student = template.copy()
    draw = ImageDraw.Draw(student)
    for left, top, right, bottom in boxes:
        draw.line((left * 1.2 + 30, top * .8 + 15,
                   right * 1.2 - 20, bottom * .8 - 12), fill="black", width=3)
    student.save(tmp_path / "student.png")
    return artifact


def _worker_render(tmp_path, monkeypatch, payload, artifact):
    from desktop.worker.mathagent_worker.preprocessing import _materialize_candidate

    rendered = []
    real_builder = evidence.build_full_column_difference_evidence_from_aligned

    def capture(*args, **kwargs):
        result = real_builder(*args, **kwargs)
        rendered.append(result)
        return result

    monkeypatch.setattr(
        template_evidence_matcher,
        "build_full_column_difference_evidence_from_aligned",
        capture,
    )
    monkeypatch.setattr(evidence, "build_full_column_difference_evidence_from_aligned", capture)
    # Known synthetic alignment only; warp, replay checks, difference pixels,
    # label renderer and manifest generation all execute the real worker code.
    candidate = {"page_type_id": "page_1", "homography": [
        [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]}
    result, aligned, mask, paths = _materialize_candidate(
        tmp_path / "student.png", tmp_path / artifact["template_image"],
        payload, candidate, tmp_path / "worker")
    assert aligned.is_file() and mask.is_file() and all(path.is_file() for path in paths)
    assert result["local_quality_report"]["layout_snapshot_sha256"] == payload["layout_snapshot_sha256"]
    assert len(rendered) == 1
    return rendered[0]


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("kind", ["fill", "solve"])
@pytest.mark.parametrize("columns", [1, 2])
def test_frozen_slots_survive_enqueue_persistence_and_worker_render(
        tmp_path, monkeypatch, enqueue, mode, kind, columns):
    artifact = _frozen_artifact(tmp_path, mode=mode, kind=kind, columns=columns)
    frozen = copy.deepcopy(artifact)
    payload = enqueue(artifact)
    layout = payload["layout_snapshot"]
    for key in ("template_answer_regions", "question_schema", "template_mode",
                "question_label_policy", "template_handwriting_regions", "question_crop_plan"):
        assert layout.get(key) == frozen[key], key
    assert payload["layout_snapshot_sha256"] == jobs.sha256_json(layout)
    rendered = _worker_render(tmp_path, monkeypatch, payload, artifact)
    placements = [p for col in rendered.diagnostics["evidence_columns"]
                  for p in col["label_placements"]]
    expected = {slot_id: box for item in frozen["template_answer_regions"]
                for slot_id, box in zip(item["slot_ids"], item["regions"])}
    assert len(placements) == len(expected)
    assert {p["slot_id"]: p["region_bbox_1000"] for p in placements} == expected
    assert artifact == frozen
    assert len(rendered.evidence_paths) == columns


def test_prewhitened_cpu_baseline_and_raw_worker_baseline_agree_for_saved_exemptions(
        tmp_path, monkeypatch, enqueue):
    artifact = _frozen_artifact(tmp_path, prewhitened=True)
    payload = enqueue(artifact)
    cpu_builder = evidence.build_full_column_difference_evidence_from_aligned
    remote = _worker_render(tmp_path, monkeypatch, payload, artifact)
    with Image.open(tmp_path / "student.png") as image:
        pixels = np.asarray(image.convert("RGB"))
    with Image.open(tmp_path / artifact["difference_template_image"]) as image:
        baseline = np.asarray(image.convert("RGB"))
    with Image.open(tmp_path / artifact["template_image"]) as image:
        assert not np.array_equal(np.asarray(image.convert("RGB")), baseline)
    cpu = cpu_builder(pixels, np.full(pixels.shape[:2], 255, np.uint8), baseline,
                      {**payload["layout_snapshot"], "template_baseline_prewhitened": True},
                      tmp_path / "cpu-evidence")
    assert cpu.diagnostics["evidence_columns"] == remote.diagnostics["evidence_columns"]
    for cpu_path, remote_path in zip(cpu.evidence_paths, remote.evidence_paths):
        with Image.open(cpu_path) as left, Image.open(remote_path) as right:
            assert np.array_equal(np.asarray(left), np.asarray(right))


def test_slot_geometry_is_part_of_persistent_job_identity(tmp_path, enqueue):
    artifact = _frozen_artifact(tmp_path)
    truncated = copy.deepcopy(artifact)
    for key in ("template_answer_regions", "template_mode", "question_label_policy"):
        truncated.pop(key)
    old_bad_job = enqueue(truncated)
    first = enqueue(artifact)
    assert first["job_id"] != old_bad_job["job_id"]
    assert first["layout_snapshot_sha256"] != old_bad_job["layout_snapshot_sha256"]
    assert enqueue(copy.deepcopy(artifact))["job_id"] == first["job_id"]
    changed = copy.deepcopy(artifact)
    changed["template_answer_regions"][0]["regions"][0][0] += 1
    second = enqueue(changed)
    assert second["job_id"] != first["job_id"]
    assert second["layout_snapshot_sha256"] != first["layout_snapshot_sha256"]


@pytest.mark.parametrize("invalid", ["duplicate", "foreign", "count", "null_ids",
    "conflict", "kind_conflict", "missing_question", "null_regions", "bad_geometry",
    "invalid_mode", "invalid_policy"])
def test_invalid_frozen_mapping_rejected_before_job_insert(tmp_path, enqueue, invalid):
    artifact = _frozen_artifact(tmp_path)
    region = artifact["template_answer_regions"][0]
    if invalid == "duplicate":
        region["slot_ids"][1] = region["slot_ids"][0]
    elif invalid == "foreign":
        region["slot_ids"][0] = "3[3]"
    elif invalid == "count":
        region["slot_ids"].pop()
    elif invalid == "null_ids":
        region["slot_ids"] = None
    elif invalid == "conflict":
        region["slot_ids"].reverse()
    elif invalid == "kind_conflict":
        region["region_kinds"][0] = "drawing"
    elif invalid == "missing_question":
        artifact["template_answer_regions"] = []
    elif invalid == "null_regions":
        artifact["template_answer_regions"] = None
    elif invalid == "bad_geometry":
        region["regions"][0][2] = 1001
    elif invalid == "invalid_mode":
        artifact["template_mode"] = "unknown"
    elif invalid == "invalid_policy":
        artifact["question_label_policy"] = "unknown"
    with pytest.raises(ValueError):
        enqueue(artifact)
    with enqueue.Session() as db:
        assert db.query(AcceleratorJob).count() == 0


def test_legacy_without_slot_metadata_keeps_existing_label_contract(tmp_path, monkeypatch, enqueue):
    artifact = _frozen_artifact(tmp_path, kind="fill")
    for item in artifact["question_schema"] + artifact["template_answer_regions"]:
        item.pop("slot_ids")
        item.pop("region_kinds")
    for key in ("template_mode", "question_label_policy", "template_answer_regions"):
        artifact.pop(key)
    # An old full-column job only had handwriting exemptions, not slot IDs.
    artifact["template_handwriting_regions"] = [
        {"question_number": "2", "regions": [[250, 100, 410, 180]]}]
    payload = enqueue(artifact)
    layout = payload["layout_snapshot"]
    assert not ({"template_answer_regions", "template_mode", "question_label_policy"} & layout.keys())
    assert "slot_ids" not in layout["question_schema"][0]
    rendered = _worker_render(tmp_path, monkeypatch, payload, artifact)
    placements = [p for col in rendered.diagnostics["evidence_columns"]
                  for p in col["label_placements"]]
    assert placements and all("slot_id" not in p for p in placements)
