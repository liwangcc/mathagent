from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from experiments.template_ocr_lab.core import (
    EvidenceResult,
    LabConfig,
    QuestionRegion,
    RegionSet,
)
from experiments.template_ocr_lab.recognition import (
    RecognitionContractError,
    TRANSCRIPTION_VISION_POLICY,
    Transcription,
    build_transcription_prompt,
    parse_transcription_response,
    recognize_evidence,
    recognize_region_sheets,
    reconcile_transcriptions,
)


def _response(numbers: list[str], answers: list[str]) -> str:
    return json.dumps({
        "questions": [
            {"question_number": number, "student_answer": answer}
            for number, answer in zip(numbers, answers)
        ]
    })


def test_prompt_contains_only_fixed_question_anchors() -> None:
    prompt = build_transcription_prompt(["19", "20"])
    assert '题号顺序：["19", "20"]' in prompt
    assert prompt.count('"question_number":"19"') == 1
    assert prompt.count('"question_number":"20"') == 1
    assert '"question_number":"1"' not in prompt
    assert "不要解题" in prompt
    assert "标准答案" not in prompt
    assert "question_text" not in prompt


def test_parser_preserves_wrong_answer_and_rejects_loose_output() -> None:
    parsed = parse_transcription_response(
        _response(["23"], ["(0,2)"]),
        ["23"],
    )
    assert parsed == [Transcription("23", "(0,2)")]

    with pytest.raises(RecognitionContractError, match="strict JSON"):
        parse_transcription_response(
            f"```json\n{_response(['23'], ['(0,2)'])}\n```",
            ["23"],
        )


def test_parser_rejects_duplicate_json_keys() -> None:
    raw = (
        '{"questions":[{"question_number":"1","student_answer":"A"}],'
        '"questions":[{"question_number":"1","student_answer":"B"}]}'
    )

    with pytest.raises(RecognitionContractError, match="duplicate JSON key"):
        parse_transcription_response(raw, ["1"])


@pytest.mark.parametrize(
    "raw",
    [
        _response(["2", "1"], ["B", "A"]),
        _response(["1", "1"], ["A", "B"]),
        _response(["1"], ["A"]),
        json.dumps({
            "questions": [
                {"question_number": 1, "student_answer": "A"},
                {"question_number": "2", "student_answer": "B"},
            ]
        }),
    ],
)
def test_parser_rejects_any_question_alignment_change(raw: str) -> None:
    with pytest.raises(RecognitionContractError):
        parse_transcription_response(raw, ["1", "2"])


def test_reconciliation_reviews_only_cross_evidence_conflicts() -> None:
    result = reconcile_transcriptions(
        [
            Transcription("1", "A"),
            Transcription("2", ""),
            Transcription("3", "AB"),
        ],
        [
            {
                "question_number": "1",
                "question_type": "choice",
                "state": "visible",
                "edge_ink_pixels": 0,
                "edge_ink_ratio": 0.0,
            },
            {
                "question_number": "2",
                "question_type": "fill",
                "state": "blank",
                "edge_ink_pixels": 0,
                "edge_ink_ratio": 0.0,
            },
            {
                "question_number": "3",
                "question_type": "choice",
                "state": "visible",
                "edge_ink_pixels": 12,
                "edge_ink_ratio": 0.25,
            },
        ],
        answer_preservation_safe=True,
    )
    assert [item["decision"] for item in result["questions"]] == [
        "accept",
        "accept",
        "review",
    ]
    assert result["review_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert result["questions"][2]["review_reasons"] == [
        "ink_touches_region_boundary",
        "noncanonical_choice_answer",
    ]


def test_reconciliation_requires_boundary_metrics_and_nonempty_input() -> None:
    with pytest.raises(RecognitionContractError, match="at least one"):
        reconcile_transcriptions(
            [],
            [],
            answer_preservation_safe=True,
        )

    with pytest.raises(RecognitionContractError, match="boundary evidence is missing"):
        reconcile_transcriptions(
            [Transcription("1", "A")],
            [
                {
                    "question_number": "1",
                    "question_type": "choice",
                    "state": "visible",
                }
            ],
            answer_preservation_safe=True,
        )


def test_reconciliation_never_accepts_consensus_template_evidence() -> None:
    result = reconcile_transcriptions(
        [Transcription("1", "A"), Transcription("2", "")],
        [
            {
                "question_number": "1",
                "question_type": "choice",
                "state": "visible",
                "edge_ink_pixels": 0,
                "edge_ink_ratio": 0.0,
            },
            {
                "question_number": "2",
                "question_type": "fill",
                "state": "blank",
                "edge_ink_pixels": 0,
                "edge_ink_ratio": 0.0,
            },
        ],
        answer_preservation_safe=False,
    )

    assert result["review_rate"] == 1.0
    assert all(
        item["review_reasons"] == ["unsafe_artifact_source"]
        for item in result["questions"]
    )


def test_sheet_runner_keeps_group_order(tmp_path: Path) -> None:
    sheets = [tmp_path / "one.png", tmp_path / "two.png"]
    for path in sheets:
        path.write_bytes(b"not decoded by the injected fake")

    policies = []

    async def fake_call(path: Path, _prompt: str, policy) -> str:
        await asyncio.sleep(0)
        policies.append(policy)
        return (
            _response(["1", "2"], ["D", ""])
            if path.name == "one.png"
            else _response(["3"], ["4"])
        )

    result = asyncio.run(
        recognize_region_sheets(
            sheets,
            [["1", "2"], ["3"]],
            fake_call,
            concurrency=2,
        )
    )
    assert result == [
        [Transcription("1", "D"), Transcription("2", "")],
        [Transcription("3", "4")],
    ]
    assert policies == [
        TRANSCRIPTION_VISION_POLICY,
        TRANSCRIPTION_VISION_POLICY,
    ]
    assert TRANSCRIPTION_VISION_POLICY.enable_thinking is False
    assert TRANSCRIPTION_VISION_POLICY.temperature == 0.0
    assert TRANSCRIPTION_VISION_POLICY.strict_json is True


def test_evidence_runner_derives_unsafe_artifact_review(
    tmp_path: Path,
) -> None:
    ink_mask = np.zeros((80, 120), dtype=np.uint8)
    ink_mask[20:35, 30:55] = 255
    evidence = EvidenceResult(
        aligned_rgb=np.full((80, 120, 3), 255, dtype=np.uint8),
        cleaned_gray=np.full((80, 120), 255, dtype=np.uint8),
        ink_mask=ink_mask,
        ambiguous_mask=np.zeros_like(ink_mask),
        evidence_rgb=np.full((80, 120, 3), 255, dtype=np.uint8),
        stats={
            "sample_id": "S01",
            "source_mode": "student_consensus",
            "answer_preservation_safe": False,
            "artifact_registration_sha256": "a" * 64,
            "config": asdict(LabConfig()),
        },
    )
    regions = RegionSet(
        "a" * 64,
        (QuestionRegion("1", "choice", (0, 0, 1000, 1000)),),
    )

    async def fake_call(_path: Path, _prompt: str, _policy) -> str:
        return _response(["1"], ["A"])

    result = asyncio.run(
        recognize_evidence(
            evidence,
            regions,
            tmp_path / "sheets",
            fake_call,
        )
    )

    assert result["answer_preservation_safe"] is False
    assert result["sample_id"] == "S01"
    assert result["artifact_registration_sha256"] == "a" * 64
    assert result["region_contract_sha256"] == regions.contract_sha256
    assert result["run_id"].startswith("run-")
    assert result["sheet_count"] == 1
    assert result["review_rate"] == 1.0
    assert result["questions"][0]["review_reasons"] == [
        "unsafe_artifact_source",
    ]


def test_evidence_runner_rejects_regions_from_another_artifact(
    tmp_path: Path,
) -> None:
    evidence = EvidenceResult(
        aligned_rgb=np.full((40, 40, 3), 255, dtype=np.uint8),
        cleaned_gray=np.full((40, 40), 255, dtype=np.uint8),
        ink_mask=np.zeros((40, 40), dtype=np.uint8),
        ambiguous_mask=np.zeros((40, 40), dtype=np.uint8),
        evidence_rgb=np.full((40, 40, 3), 255, dtype=np.uint8),
        stats={
            "sample_id": "S01",
            "source_mode": "verified_blank",
            "answer_preservation_safe": True,
            "artifact_registration_sha256": "a" * 64,
            "config": asdict(LabConfig()),
        },
    )
    regions = RegionSet(
        "b" * 64,
        (QuestionRegion("1", "fill", (0, 0, 1000, 1000)),),
    )

    async def unused_call(_path: Path, _prompt: str, _policy) -> str:
        raise AssertionError("mismatched regions must fail before OCR")

    with pytest.raises(
        RecognitionContractError,
        match="do not belong to this template artifact",
    ):
        asyncio.run(
            recognize_evidence(
                evidence,
                regions,
                tmp_path / "sheets",
                unused_call,
            )
        )


def test_concurrent_evidence_runs_never_overwrite_sheets(
    tmp_path: Path,
) -> None:
    artifact_hash = "c" * 64
    regions = RegionSet(
        artifact_hash,
        (QuestionRegion("1", "choice", (0, 0, 1000, 1000)),),
    )

    def make_evidence(left: bool) -> EvidenceResult:
        ink_mask = np.zeros((80, 160), dtype=np.uint8)
        if left:
            ink_mask[25:40, 20:40] = 255
        else:
            ink_mask[25:40, 120:140] = 255
        return EvidenceResult(
            aligned_rgb=np.full((80, 160, 3), 255, dtype=np.uint8),
            cleaned_gray=np.full((80, 160), 255, dtype=np.uint8),
            ink_mask=ink_mask,
            ambiguous_mask=np.zeros_like(ink_mask),
            evidence_rgb=np.full((80, 160, 3), 255, dtype=np.uint8),
            stats={
                "sample_id": "LEFT" if left else "RIGHT",
                "source_mode": "verified_blank",
                "answer_preservation_safe": True,
                "artifact_registration_sha256": artifact_hash,
                "config": asdict(LabConfig()),
            },
        )

    seen_paths: list[Path] = []

    async def delayed_read(path: Path, _prompt: str, _policy) -> str:
        seen_paths.append(path)
        await asyncio.sleep(0.02)
        image = np.asarray(Image.open(path).convert("L"))
        ink_y, ink_x = np.where(image[:, 170:] < 100)
        del ink_y
        answer = "A" if float(np.mean(ink_x)) < 220 else "B"
        return _response(["1"], [answer])

    async def run_both():
        return await asyncio.gather(
            recognize_evidence(
                make_evidence(True),
                regions,
                tmp_path / "shared-sheets",
                delayed_read,
            ),
            recognize_evidence(
                make_evidence(False),
                regions,
                tmp_path / "shared-sheets",
                delayed_read,
            ),
        )

    left_result, right_result = asyncio.run(run_both())

    assert left_result["questions"][0]["student_answer"] == "A"
    assert right_result["questions"][0]["student_answer"] == "B"
    assert left_result["sample_id"] == "LEFT"
    assert right_result["sample_id"] == "RIGHT"
    assert seen_paths[0].parent != seen_paths[1].parent
