from __future__ import annotations

import pytest

from experiments.template_ocr_lab.evaluation import evaluate_quality_gate


ARTIFACT_HASH = "a" * 64
REGION_HASH = "b" * 64


def _sample(index: int) -> dict:
    return {
        "sample_id": f"S{index:02d}",
        "run_id": f"run-{index:02d}",
        "artifact_registration_sha256": ARTIFACT_HASH,
        "region_contract_sha256": REGION_HASH,
        "expected_score": 40,
        "predicted_score": 40,
        "question_count": 25,
        "review_count": 2,
        "transcription_evaluated_count": 25,
        "transcription_correct_count": 25,
        "question_number_shift_count": 0,
        "silent_uplift_count": 0,
        "ocr_contract_failure_count": 0,
    }


def test_quality_gate_passes_only_complete_safe_metrics() -> None:
    result = evaluate_quality_gate(
        [_sample(index) for index in range(1, 11)],
        artifact={
            "source_mode": "verified_blank",
            "answer_preservation_safe": True,
            "artifact_registration_sha256": ARTIFACT_HASH,
            "region_contract_sha256": REGION_HASH,
        },
        alignment={
            "artifact_registration_sha256": ARTIFACT_HASH,
            "alignment_pass_rate": 1.0,
            "min_template_edge_recall": 0.88,
        },
    )
    assert result["gate_pass"] is True
    assert all(result["checks"].values())
    assert result["metrics"]["manual_review_rate"] == 0.08
    assert result["metrics"]["transcription_accuracy"] == 1.0


def test_quality_gate_rejects_consensus_uplift_shift_and_excess_review() -> None:
    samples = [_sample(index) for index in range(1, 11)]
    samples[0] = {
        **samples[0],
        "predicted_score": 44,
        "review_count": 10,
        "transcription_correct_count": 23,
        "question_number_shift_count": 1,
        "silent_uplift_count": 1,
    }
    result = evaluate_quality_gate(
        samples,
        artifact={
            "source_mode": "student_consensus",
            "answer_preservation_safe": False,
            "artifact_registration_sha256": ARTIFACT_HASH,
            "region_contract_sha256": REGION_HASH,
        },
        alignment={
            "artifact_registration_sha256": ARTIFACT_HASH,
            "alignment_pass_rate": 1.0,
            "min_template_edge_recall": 0.88,
        },
    )
    assert result["gate_pass"] is False
    assert result["checks"]["verified_blank_artifact"] is False
    assert result["checks"]["question_number_shifts"] is False
    assert result["checks"]["silent_uplifts"] is False
    assert result["checks"]["score_uplift"] is False
    assert result["metrics"]["max_score_uplift"] == 4


def test_quality_gate_rejects_selective_transcription_scoring() -> None:
    samples = [_sample(index) for index in range(1, 11)]
    samples[0] = {
        **samples[0],
        "transcription_evaluated_count": 20,
        "transcription_correct_count": 20,
    }

    with pytest.raises(
        ValueError,
        match="transcription_evaluated_count must equal question_count",
    ):
        evaluate_quality_gate(
            samples,
            artifact={
                "source_mode": "verified_blank",
                "answer_preservation_safe": True,
                "artifact_registration_sha256": ARTIFACT_HASH,
                "region_contract_sha256": REGION_HASH,
            },
            alignment={
                "artifact_registration_sha256": ARTIFACT_HASH,
                "alignment_pass_rate": 1.0,
                "min_template_edge_recall": 0.88,
            },
        )


def test_quality_gate_rejects_catastrophic_score_drop() -> None:
    samples = [_sample(index) for index in range(1, 11)]
    samples[0] = {
        **samples[0],
        "predicted_score": 0,
    }

    result = evaluate_quality_gate(
        samples,
        artifact={
            "source_mode": "verified_blank",
            "answer_preservation_safe": True,
            "artifact_registration_sha256": ARTIFACT_HASH,
            "region_contract_sha256": REGION_HASH,
        },
        alignment={
            "artifact_registration_sha256": ARTIFACT_HASH,
            "alignment_pass_rate": 1.0,
            "min_template_edge_recall": 0.88,
        },
    )

    assert result["gate_pass"] is False
    assert result["checks"]["score_downward"] is False
    assert result["metrics"]["max_score_downward"] == -40


def test_quality_gate_rejects_mixed_artifacts_and_duplicate_identity() -> None:
    samples = [_sample(index) for index in range(1, 11)]
    samples[0] = {
        **samples[0],
        "artifact_registration_sha256": "c" * 64,
    }
    with pytest.raises(ValueError, match="different template artifact"):
        evaluate_quality_gate(
            samples,
            artifact={
                "source_mode": "verified_blank",
                "answer_preservation_safe": True,
                "artifact_registration_sha256": ARTIFACT_HASH,
                "region_contract_sha256": REGION_HASH,
            },
            alignment={
                "artifact_registration_sha256": ARTIFACT_HASH,
                "alignment_pass_rate": 1.0,
                "min_template_edge_recall": 0.88,
            },
        )

    samples = [_sample(index) for index in range(1, 11)]
    samples[1] = {
        **samples[1],
        "sample_id": "s01",
    }
    with pytest.raises(ValueError, match="sample_id values must be unique"):
        evaluate_quality_gate(
            samples,
            artifact={
                "source_mode": "verified_blank",
                "answer_preservation_safe": True,
                "artifact_registration_sha256": ARTIFACT_HASH,
                "region_contract_sha256": REGION_HASH,
            },
            alignment={
                "artifact_registration_sha256": ARTIFACT_HASH,
                "alignment_pass_rate": 1.0,
                "min_template_edge_recall": 0.88,
            },
        )
