import copy

import pytest

from services.template_anchor_crops import (
    AnchorCropPlanError,
    PLAN_STRATEGY,
    build_anchor_crop_plan,
    validate_anchor_crop_plan,
)


ARTIFACT_SHA256 = "a" * 64


def _layout(boundaries: list[int]) -> dict:
    columns = [
        {
            "column_index": index,
            "x1_1000": left,
            "x2_1000": right,
        }
        for index, (left, right) in enumerate(
            zip(boundaries, boundaries[1:]),
            1,
        )
    ]
    return {
        "column_layout": {
            "column_count": len(columns),
            "columns": columns,
        },
        "pages": [{"page_index": 0, "candidates": []}],
    }


@pytest.mark.parametrize(
    ("boundaries", "expected_boxes"),
    [
        ([0, 1000], [[0, 0, 1000, 1000]]),
        ([0, 430, 1000], [[0, 0, 430, 1000], [430, 0, 1000, 1000]]),
        (
            [0, 330, 665, 1000],
            [
                [0, 0, 330, 1000],
                [330, 0, 665, 1000],
                [665, 0, 1000, 1000],
            ],
        ),
    ],
)
def test_build_plan_freezes_one_to_three_full_height_columns(
    boundaries,
    expected_boxes,
):
    plan = build_anchor_crop_plan(
        _layout(boundaries),
        artifact_sha256=ARTIFACT_SHA256,
        expected_question_numbers=["1", "2"],
    )

    assert plan["version"] == 2
    assert plan["strategy"] == PLAN_STRATEGY == "full_height_columns"
    assert plan["parameters"]["column_count"] == len(expected_boxes)
    assert [block["bbox_1000"] for block in plan["blocks"]] == expected_boxes
    assert [block["block_id"] for block in plan["blocks"]] == [
        f"col_{index}" for index in range(1, len(expected_boxes) + 1)
    ]
    assert plan["coverage"] == {
        "columns": [f"col_{index}" for index in range(1, len(expected_boxes) + 1)],
        "page_top_to_bottom": True,
        "vertical_gaps": 0,
        "block_count": len(expected_boxes),
        "anchor_count": 0,
    }
    assert plan["expected_question_numbers"] == ["1", "2"]
    assert plan["anchors"] == []


@pytest.mark.parametrize(
    "column_layout",
    [
        {"column_count": True, "columns": []},
        {"column_count": 4, "columns": []},
        {
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            ],
        },
        {
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 10, "x2_1000": 500},
                {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
            ],
        },
        {
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 490},
                {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
            ],
        },
        {
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 510},
                {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
            ],
        },
        {
            "column_count": 2,
            "columns": [
                {"column_index": 2, "x1_1000": 0, "x2_1000": 500},
                {"column_index": 1, "x1_1000": 500, "x2_1000": 1000},
            ],
        },
    ],
)
def test_build_plan_rejects_invalid_column_contract(column_layout):
    with pytest.raises(AnchorCropPlanError):
        build_anchor_crop_plan(
            {"column_layout": column_layout},
            artifact_sha256=ARTIFACT_SHA256,
        )


def test_validate_rejects_source_or_block_contract_changes():
    plan = build_anchor_crop_plan(
        _layout([0, 330, 665, 1000]),
        artifact_sha256=ARTIFACT_SHA256,
    )

    with pytest.raises(AnchorCropPlanError, match="source hash mismatch"):
        validate_anchor_crop_plan(plan, artifact_sha256="b" * 64)

    provider_bottom = copy.deepcopy(plan)
    provider_bottom["provider_bottom_ignored"] = False
    with pytest.raises(AnchorCropPlanError, match="provider bottom"):
        validate_anchor_crop_plan(provider_bottom, artifact_sha256=ARTIFACT_SHA256)

    gap = copy.deepcopy(plan)
    gap["parameters"]["columns"][1]["x1_1000"] = 340
    with pytest.raises(AnchorCropPlanError, match="without gaps"):
        validate_anchor_crop_plan(gap, artifact_sha256=ARTIFACT_SHA256)

    swapped = copy.deepcopy(plan)
    swapped["blocks"][0], swapped["blocks"][1] = (
        swapped["blocks"][1],
        swapped["blocks"][0],
    )
    with pytest.raises(AnchorCropPlanError, match="does not match"):
        validate_anchor_crop_plan(swapped, artifact_sha256=ARTIFACT_SHA256)
