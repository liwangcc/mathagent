"""Standalone template-guided OCR experiments.

Nothing in this package is imported by the production application.  The lab is
kept separate until its quality gates are met on representative papers.
"""

from .core import (
    AlignmentError,
    LabConfig,
    QuestionRegion,
    RegionSet,
    TemplateMatcher,
    benchmark_artifact,
    compile_blank_template,
    compile_consensus_template,
    extract_evidence,
    load_regions,
    load_verified_manifest,
    render_region_sheets,
    summarize_regions,
    validate_sample_id,
)
from .recognition import (
    RecognitionContractError,
    TRANSCRIPTION_VISION_POLICY,
    Transcription,
    VisionPolicy,
    build_transcription_prompt,
    parse_transcription_response,
    recognize_evidence,
)
from .evaluation import evaluate_quality_gate

__all__ = [
    "AlignmentError",
    "LabConfig",
    "QuestionRegion",
    "RegionSet",
    "TemplateMatcher",
    "benchmark_artifact",
    "compile_blank_template",
    "compile_consensus_template",
    "extract_evidence",
    "load_regions",
    "load_verified_manifest",
    "render_region_sheets",
    "summarize_regions",
    "validate_sample_id",
    "RecognitionContractError",
    "TRANSCRIPTION_VISION_POLICY",
    "Transcription",
    "VisionPolicy",
    "build_transcription_prompt",
    "parse_transcription_response",
    "recognize_evidence",
    "evaluate_quality_gate",
]
