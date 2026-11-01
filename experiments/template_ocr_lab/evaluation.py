from __future__ import annotations

import math
from typing import Sequence

from .core import validate_sample_id


def _integer(record: dict, name: str, *, minimum: int = 0) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _ratio(record: dict, name: str) -> float:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _sha256(record: dict, name: str) -> str:
    value = record.get(name)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return value.casefold()


def evaluate_quality_gate(
    samples: Sequence[dict],
    *,
    artifact: dict,
    alignment: dict,
    minimum_samples: int = 10,
    minimum_transcription_accuracy: float = 0.98,
    maximum_review_rate: float = 0.10,
    maximum_score_downward: int = 4,
) -> dict:
    """Evaluate whether an isolated recognizer is eligible for integration.

    Scores are supporting diagnostics.  The hard safety checks are based on
    verified blank-template provenance, exact question ownership, no silent
    uplift, per-question transcription accuracy, and bounded manual review.
    """
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be positive")
    if not 0 <= minimum_transcription_accuracy <= 1:
        raise ValueError("minimum_transcription_accuracy must be between 0 and 1")
    if not 0 <= maximum_review_rate <= 1:
        raise ValueError("maximum_review_rate must be between 0 and 1")
    if (
        isinstance(maximum_score_downward, bool)
        or not isinstance(maximum_score_downward, int)
        or maximum_score_downward < 0
    ):
        raise ValueError("maximum_score_downward must be a non-negative integer")
    if not isinstance(artifact, dict) or not isinstance(alignment, dict):
        raise ValueError("artifact and alignment must be objects")
    if not samples:
        raise ValueError("at least one sample result is required")

    artifact_hash = _sha256(artifact, "artifact_registration_sha256")
    region_hash = _sha256(artifact, "region_contract_sha256")
    if _sha256(alignment, "artifact_registration_sha256") != artifact_hash:
        raise ValueError("alignment belongs to a different template artifact")

    sample_ids: set[str] = set()
    run_ids: set[str] = set()
    score_differences: list[int] = []
    exact_scores = 0
    total_questions = 0
    total_reviews = 0
    total_transcription_correct = 0
    total_transcription_evaluated = 0
    total_shifts = 0
    total_silent_uplifts = 0
    total_contract_failures = 0

    for record in samples:
        if not isinstance(record, dict):
            raise ValueError("each sample result must be an object")
        sample_id = validate_sample_id(record.get("sample_id"))
        normalized_sample_id = sample_id.casefold()
        if normalized_sample_id in sample_ids:
            raise ValueError("sample_id values must be unique")
        sample_ids.add(normalized_sample_id)
        run_id = validate_sample_id(record.get("run_id"))
        if not run_id.startswith("run-"):
            raise ValueError("run_id must start with 'run-'")
        normalized_run_id = run_id.casefold()
        if normalized_run_id in run_ids:
            raise ValueError("run_id values must be unique")
        run_ids.add(normalized_run_id)
        if _sha256(record, "artifact_registration_sha256") != artifact_hash:
            raise ValueError("sample belongs to a different template artifact")
        if _sha256(record, "region_contract_sha256") != region_hash:
            raise ValueError("sample belongs to a different region contract")

        expected_score = _integer(record, "expected_score")
        predicted_score = _integer(record, "predicted_score")
        question_count = _integer(record, "question_count", minimum=1)
        review_count = _integer(record, "review_count")
        transcription_evaluated = _integer(
            record,
            "transcription_evaluated_count",
            minimum=1,
        )
        transcription_correct = _integer(
            record,
            "transcription_correct_count",
        )
        if review_count > question_count:
            raise ValueError("review_count cannot exceed question_count")
        if transcription_correct > transcription_evaluated:
            raise ValueError(
                "transcription_correct_count cannot exceed evaluated count"
            )
        if transcription_evaluated != question_count:
            raise ValueError(
                "transcription_evaluated_count must equal question_count"
            )

        difference = predicted_score - expected_score
        score_differences.append(difference)
        exact_scores += int(difference == 0)
        total_questions += question_count
        total_reviews += review_count
        total_transcription_evaluated += transcription_evaluated
        total_transcription_correct += transcription_correct
        total_shifts += _integer(record, "question_number_shift_count")
        total_silent_uplifts += _integer(record, "silent_uplift_count")
        total_contract_failures += _integer(record, "ocr_contract_failure_count")

    alignment_pass_rate = _ratio(alignment, "alignment_pass_rate")
    min_edge_recall = _ratio(alignment, "min_template_edge_recall")
    transcription_accuracy = (
        total_transcription_correct / total_transcription_evaluated
    )
    review_rate = total_reviews / total_questions
    max_score_uplift = max(max(score_differences), 0)
    max_score_downward = min(min(score_differences), 0)

    checks = {
        "minimum_sample_count": len(samples) >= minimum_samples,
        "verified_blank_artifact": (
            artifact.get("source_mode") == "verified_blank"
            and artifact.get("answer_preservation_safe") is True
        ),
        "alignment_pass_rate": alignment_pass_rate >= 0.95,
        "minimum_edge_recall": min_edge_recall >= 0.72,
        "ocr_contract_failures": total_contract_failures == 0,
        "question_number_shifts": total_shifts == 0,
        "silent_uplifts": total_silent_uplifts == 0,
        "score_uplift": max_score_uplift == 0,
        "score_downward": abs(max_score_downward) <= maximum_score_downward,
        "transcription_accuracy": (
            transcription_accuracy >= minimum_transcription_accuracy
        ),
        "manual_review_rate": review_rate <= maximum_review_rate,
    }
    return {
        "gate_pass": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "minimum_samples": minimum_samples,
            "minimum_transcription_accuracy": minimum_transcription_accuracy,
            "maximum_review_rate": maximum_review_rate,
            "maximum_score_downward": maximum_score_downward,
            "minimum_alignment_pass_rate": 0.95,
            "minimum_template_edge_recall": 0.72,
        },
        "metrics": {
            "sample_count": len(samples),
            "exact_score_rate": round(exact_scores / len(samples), 4),
            "mean_absolute_score_error": round(
                sum(abs(value) for value in score_differences) / len(samples),
                4,
            ),
            "mean_score_difference": round(
                sum(score_differences) / len(samples),
                4,
            ),
            "max_score_uplift": max_score_uplift,
            "max_score_downward": max_score_downward,
            "question_number_shift_count": total_shifts,
            "silent_uplift_count": total_silent_uplifts,
            "ocr_contract_failure_count": total_contract_failures,
            "transcription_correct_count": total_transcription_correct,
            "transcription_evaluated_count": total_transcription_evaluated,
            "transcription_accuracy": round(transcription_accuracy, 4),
            "review_count": total_reviews,
            "question_count": total_questions,
            "manual_review_rate": round(review_rate, 4),
            "alignment_pass_rate": alignment_pass_rate,
            "min_template_edge_recall": min_edge_recall,
        },
    }
