import asyncio
import json
import math
import os

import pytest

np = pytest.importorskip("numpy")
from PIL import Image

from services import template_evidence
from services.template_evidence import (
    TemplateEvidenceError,
    build_template_artifact,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
    template_confidence_diagnostics,
    verify_template_artifacts,
)


ANCHORS = [
    {
        "question_number": "1",
        "question_number_bbox": [100, 100, 140, 125],
    },
    {
        "question_number": "2",
        "question_number_bbox": [100, 300, 140, 325],
    },
]

HANDWRITING_REGIONS = [
    {"question_number": "1", "regions": [[350, 220, 390, 270]]},
    {"question_number": "2", "regions": []},
]

PADDED_HANDWRITING_REGIONS = [
    {"question_number": "1", "regions": [[350, 220, 394, 270]]},
    {"question_number": "2", "regions": []},
]


def test_question_anchor_contract_rejects_duplicate_or_bad_boxes():
    assert normalize_question_anchors(ANCHORS) == ANCHORS
    with pytest.raises(ValueError, match="duplicate"):
        normalize_question_anchors(ANCHORS + [dict(ANCHORS[0])])
    with pytest.raises(ValueError, match="invalid area"):
        normalize_question_anchors([{
            **ANCHORS[0],
            "question_number_bbox": [100, 100, 100, 125],
        }])
    assert normalize_template_handwriting_regions(HANDWRITING_REGIONS) == (
        HANDWRITING_REGIONS
    )
    with pytest.raises(ValueError, match="duplicate"):
        normalize_template_handwriting_regions(
            HANDWRITING_REGIONS + [dict(HANDWRITING_REGIONS[0])]
        )
    with pytest.raises(ValueError, match="超出页面或面积为零"):
        normalize_template_handwriting_regions([{
            "question_number": "1",
            "regions": [[100, 100, 100, 125]],
        }])


@pytest.mark.parametrize(
    "confidence",
    [math.nan, math.inf, -math.inf, True, -0.01, 1.01, "0.5"],
)
def test_template_confidence_rejects_non_finite_non_numeric_or_out_of_range(
    confidence,
):
    with pytest.raises(ValueError, match="confidence"):
        normalize_question_anchors([{
            "question_number": "1",
            "question_number_bbox": [100, 100, 140, 125],
            "confidence": confidence,
        }])
    with pytest.raises(ValueError, match="confidence"):
        normalize_template_handwriting_regions([{
            "question_number": "1",
            "regions": [[350, 220, 390, 270]],
            "confidence": confidence,
        }])


def test_empty_handwriting_regions_do_not_trigger_confidence_review():
    diagnostics = template_confidence_diagnostics(
        [],
        [{
            "question_number": "1",
            "regions": [],
            "confidence": 0.1,
        }],
    )
    assert diagnostics["needs_review"] is False
    assert diagnostics["low_confidence_handwriting_regions"] == []


def test_low_confidence_handwriting_cannot_reach_whiteout_paths():
    rgb = np.full((40, 40, 3), 80, dtype=np.uint8)
    gray = np.full((40, 40), 80, dtype=np.uint8)
    source_mask = np.full((40, 40), 255, dtype=np.uint8)
    regions = [{
        "question_number": "1",
        "regions": [[100, 100, 300, 300]],
        "confidence": 0.2,
        "confidence_source": "model",
    }]

    with pytest.raises(TemplateEvidenceError) as rgb_error:
        template_evidence._erase_template_handwriting_rgb(rgb, regions)
    assert rgb_error.value.diagnostics["review_reason"] == "low_confidence"

    with pytest.raises(TemplateEvidenceError) as gray_error:
        template_evidence._erase_template_handwriting(gray, regions)
    assert gray_error.value.diagnostics["needs_review"] is True

    with pytest.raises(TemplateEvidenceError):
        template_evidence._answer_only_pixels_v2(
            rgb,
            rgb,
            source_mask,
            template_handwriting_regions=regions,
        )


def test_teacher_confirmed_handwriting_can_bypass_model_threshold():
    gray = np.full((40, 40), 80, dtype=np.uint8)
    regions = [{
        "question_number": "1",
        "regions": [[100, 100, 300, 300]],
        "confidence": 0.1,
        "human_confirmed": True,
        "confidence_source": "teacher",
    }]
    cleaned = template_evidence._erase_template_handwriting(gray, regions)
    assert np.all(cleaned[4:12, 4:12] == 255)


def test_template_artifact_rejects_low_confidence_exemption_before_materializing(
    tmp_path,
):
    source = tmp_path / "template.png"
    Image.new("RGB", (80, 80), "white").save(source)
    with pytest.raises(TemplateEvidenceError, match="置信度"):
        build_template_artifact(
            source,
            tmp_path / "stored",
            0,
            [{
                "question_number": "1",
                "question_type": "fill",
            }],
            question_anchors=[{
                "question_number": "1",
                "question_number_bbox": [100, 100, 180, 160],
                "confidence": 0.95,
            }],
            template_handwriting_regions=[{
                "question_number": "1",
                "regions": [[200, 200, 300, 300]],
                "confidence": 0.2,
                "confidence_source": "model",
            }],
        )


def test_artifact_round_trip_preserves_question_anchors(tmp_path):
    from PIL import Image

    source = tmp_path / "template.png"
    Image.new("RGB", (1000, 700), "white").save(source)
    artifact = build_template_artifact(
        source,
        tmp_path / "template",
        0,
        [{
            "question_number": "1",
            "question_type": "fill",
            "answer_bbox": [200, 200, 500, 300],
        }],
        question_anchors=[ANCHORS[0]],
        template_answer_regions=[{
            "question_number": "1",
            "regions": [[350, 220, 390, 270]],
        }],
        template_handwriting_regions=[],
        template_mode="blank",
    )
    verified = verify_template_artifacts(
        tmp_path / "template", {"page_1": artifact}
    )
    assert verified["page_1"]["question_anchors"] == [ANCHORS[0]]
    assert verified["page_1"]["template_answer_regions"] == [
        {
            "question_number": "1",
            "regions": [[350, 220, 390, 270]],
        }
    ]
    assert verified["page_1"]["template_handwriting_regions"] == []
    assert verified["page_1"]["template_mode"] == "blank"
    assert "difference_template_image" not in verified["page_1"]
    assert "difference_template_sha256" not in verified["page_1"]


def test_blank_template_artifact_rejects_missing_answer_region(tmp_path):
    source = tmp_path / "blank-template.png"
    Image.new("RGB", (1000, 700), "white").save(source)

    with pytest.raises(ValueError, match="answer region"):
        build_template_artifact(
            source,
            tmp_path / "template",
            0,
            [{
                "question_number": "1",
                "question_type": "fill",
            }],
            question_anchors=[ANCHORS[0]],
            template_answer_regions=[{
                "question_number": "1",
                "regions": [],
            }],
            template_mode="blank",
        )


def test_teacher_deleted_number_box_keeps_identity_without_position():
    """Deleting the printed-number marker leaves an empty box: the question keeps
    its identity with no printed position; malformed boxes still fail closed."""
    from services.template_evidence import normalize_question_anchors
    anchors = normalize_question_anchors([
        {"question_number": "1", "question_number_bbox": []},
        {"question_number": "2", "question_number_bbox": [10, 20, 40, 60]},
    ])
    assert anchors[0] == {"question_number": "1"}
    assert anchors[1]["question_number_bbox"] == [10, 20, 40, 60]
    with pytest.raises(ValueError):
        normalize_question_anchors([{"question_number": "1", "question_number_bbox": [10, 20]}])
