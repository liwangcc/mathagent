"""Persisted pre-policy templates keep working without weakening new solves.

The v1 artifact fixture below records the pre-change HEAD builder's storage
contract, including its defaulted ``solve`` value. All pixels are synthetic;
tests neither require git at runtime nor read real templates/student files.
"""

import copy
import hashlib
import json

import cv2
import numpy as np
from PIL import Image
import pytest

from services import template_evidence as evidence


@pytest.fixture
def synthetic_page(tmp_path):
    pixels = np.full((700, 1000, 3), 255, dtype=np.uint8)
    rng = np.random.default_rng(42)
    for _ in range(180):
        point = (int(rng.integers(20, 980)), int(rng.integers(20, 680)))
        shade = int(rng.integers(0, 130))
        cv2.circle(pixels, point, int(rng.integers(2, 9)), (shade,) * 3, -1)
    cv2.putText(pixels, "26. SHOW YOUR WORK", (100, 220), cv2.FONT_HERSHEY_SIMPLEX,
                .8, (0, 0, 0), 2, cv2.LINE_AA)
    source = tmp_path / "synthetic-template.png"
    Image.fromarray(pixels).save(source)
    transform = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [999, 0], [999, 699], [0, 699]]),
        np.float32([[70, 45], [1050, 20], [1090, 765], [35, 790]]),
    )
    photo = tmp_path / "synthetic-photo.png"
    Image.fromarray(cv2.warpPerspective(
        pixels, transform, (1140, 820), borderValue=(255, 255, 255),
    )).save(photo)
    return source, photo


def _pre_policy_artifact(root, source, mode, *, default_already_normalized):
    target = root / "artifacts" / "page_000" / "template.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    schema = {"question_number": "26"}
    if default_already_normalized:
        # HEAD normalize_question_schema filled this even when the original
        # model/template input never declared a question type.
        schema.update(question_type="solve", choice_mode="unknown")
    regions = [{"question_number": "26", "regions": [[600, 400, 940, 700]]}]
    artifact = {
        "version": 1,
        "template_image": "artifacts/page_000/template.png",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "width": 1000, "height": 700,
        "question_schema": [schema], "question_numbers": ["26"],
        "blank_detection_enabled": True, "template_mode": mode,
        "question_anchors": [{"question_number": "26"}],
        "template_answer_regions": regions,
        "template_handwriting_regions": regions if mode == "marked" else [],
    }
    manifest = root / "legacy-artifact.json"
    manifest.write_text(json.dumps(artifact), encoding="utf-8")
    return json.loads(manifest.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("default_already_normalized", [False, True])
def test_persisted_head_artifact_without_number_box_survives_alignment(
    tmp_path, synthetic_page, mode, default_already_normalized,
):
    source, photo = synthetic_page
    root = tmp_path / "stored"
    artifact = _pre_policy_artifact(
        root, source, mode, default_already_normalized=default_already_normalized,
    )
    snapshot = copy.deepcopy(artifact)
    matcher = evidence.TemplateEvidenceMatcher({"page_1": artifact}, root)
    assert matcher.artifacts["page_1"]["question_schema"][0]["question_type"] == "solve"
    assert "question_label_policy" not in matcher.artifacts["page_1"]
    result = matcher.match_and_build(photo, tmp_path / "evidence")
    assert result.question_numbers == ["26"]
    placement = result.diagnostics["evidence_columns"][0]["label_placements"][0]
    # Unified answer geometry takes precedence in either historical mode.
    assert placement["source"] == "template_answer_region"
    assert result.diagnostics["legacy_solve_label_policy"]["question_numbers"] == ["26"]
    assert artifact == snapshot


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_pre_policy_artifact_with_printed_box_prefers_unified_region(tmp_path, synthetic_page, mode):
    source, photo = synthetic_page
    root = tmp_path / "stored"
    artifact = _pre_policy_artifact(root, source, mode, default_already_normalized=True)
    artifact["question_anchors"][0]["question_number_bbox"] = [100, 285, 147, 327]
    result = evidence.TemplateEvidenceMatcher({"page_1": artifact}, root).match_and_build(
        photo, tmp_path / "evidence",
    )
    assert result.diagnostics["evidence_columns"][0]["label_placements"][0]["source"] == "template_answer_region"
    assert "legacy_solve_label_policy" not in result.diagnostics


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_new_explicit_solve_can_use_unified_box_without_printed_anchor(tmp_path, synthetic_page, mode):
    source, _ = synthetic_page
    root = tmp_path / "new"
    artifact = evidence.build_template_artifact(
        source, root, 0, [{"question_number": "26", "question_type": "solve"}],
        template_mode=mode, question_anchors=[{"question_number": "26"}],
        template_answer_regions=[{"question_number": "26", "regions": [[600, 400, 940, 700]]}],
    )
    checked = evidence.verify_template_artifacts(root, {"page_1": artifact})
    assert checked["page_1"]["template_answer_regions"][0]["regions"] == [[600, 400, 940, 700]]


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_modern_policy_roundtrip_requires_printed_box_after_storage(tmp_path, synthetic_page, mode):
    source, photo = synthetic_page
    root = tmp_path / "new"
    artifact = evidence.build_template_artifact(
        source, root, 0, [{"question_number": "26", "question_type": "solve"}],
        template_mode=mode,
        question_anchors=[{"question_number": "26", "question_number_bbox": [100, 285, 147, 327]}],
        template_answer_regions=[{"question_number": "26", "regions": []}],
    )
    assert artifact["question_label_policy"] == evidence.PRINTED_SOLVE_LABEL_POLICY
    reloaded = json.loads(json.dumps(artifact))
    matcher = evidence.TemplateEvidenceMatcher({"page_1": reloaded}, root)
    assert matcher.artifacts["page_1"]["question_label_policy"] == evidence.PRINTED_SOLVE_LABEL_POLICY
    result = matcher.match_and_build(photo, tmp_path / "evidence")
    assert result.diagnostics["printed_solve_label_policy"]["placed_labels"] == 1
    assert "legacy_solve_label_policy" not in result.diagnostics
    reloaded["question_anchors"][0].pop("question_number_bbox")
    with pytest.raises(ValueError, match="印刷题号"):
        evidence.verify_template_artifacts(root, {"page_1": reloaded})


@pytest.mark.parametrize("policy", [None, "", "legacy", "printed-solve-v999", False])
def test_unknown_or_explicit_empty_policy_cannot_downgrade(tmp_path, synthetic_page, policy):
    source, _ = synthetic_page
    root = tmp_path / "stored"
    artifact = _pre_policy_artifact(root, source, "blank", default_already_normalized=True)
    artifact["question_label_policy"] = policy
    with pytest.raises(ValueError, match="question label policy"):
        evidence.verify_template_artifacts(root, {"page_1": artifact})


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_modern_geometry_draft_is_editable_but_never_graded_as_legacy(tmp_path, synthetic_page, mode):
    source, photo = synthetic_page
    root = tmp_path / "draft"
    # Production geometry-review drafts deliberately omit authoritative
    # anchors/regions; their model candidates live in a separate review record.
    draft = evidence.build_template_artifact(
        source, root, 0, [{"question_number": "26", "question_type": "solve"}],
        template_mode=mode,
    )
    assert draft["question_label_policy"] == evidence.PRINTED_SOLVE_LABEL_POLICY
    assert "question_anchors" not in draft
    restored = evidence.verify_template_artifacts(root, {"page_1": json.loads(json.dumps(draft))})
    matcher = evidence.TemplateEvidenceMatcher(restored, root)
    with pytest.raises(evidence.TemplateEvidenceError, match="印刷题号"):
        matcher.match_and_build(photo, tmp_path / "before-review")
    # The teacher supplies the actual printed-number position; the policy is
    # preserved rather than removing it to force an old placement route.
    draft["question_anchors"] = [{
        "question_number": "26", "question_number_bbox": [100, 285, 147, 327],
        "human_confirmed": True, "confidence_source": "teacher",
    }]
    result = evidence.TemplateEvidenceMatcher({"page_1": draft}, root).match_and_build(
        photo, tmp_path / "after-review",
    )
    assert result.diagnostics["printed_solve_label_policy"]["placed_labels"] == 1
    assert "legacy_solve_label_policy" not in result.diagnostics
