"""Local template alignment and answer-only evidence image generation.

The artifact format is intentionally small and self-verifying.  A caller keeps
the returned mapping in template metadata, then passes a mapping keyed by
``page_type_id`` to :class:`TemplateEvidenceMatcher`.
"""

from __future__ import annotations

from services.grading_contract import reconcile_drawing_contract
from services.processing_errors import TemplateSchemaError

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from services.accelerator_artifacts import validate_uploaded_artifact_file
from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_canonicalization_contract import (
    validate_canonicalization_metadata,
)
from services.template_evidence_base import (
    _LazyModule,
    cv2,
    np,
    Image,
    ImageDraw,
    ImageFont,
    ImageOps,
    MAX_IMAGE_EDGE,
    MAX_SOURCE_PIXELS,
    _PAGE_TYPE_RE,
    _SHA256_RE,
    TemplateEvidenceError,
    EvidenceResult,
)
from services.template_evidence_schema import (
    MAX_ANSWER_REGIONS,
    MAX_QUESTION_ANCHORS,
    MAX_TEMPLATE_HANDWRITING_QUESTIONS,
    MAX_TEMPLATE_HANDWRITING_BOXES,
    MAX_TEMPLATE_HANDWRITING_BOXES_PER_QUESTION,
    TEMPLATE_CONFIDENCE_GATE_VERSION,
    TEMPLATE_ANCHOR_CONFIDENCE_THRESHOLD,
    TEMPLATE_HANDWRITING_CONFIDENCE_THRESHOLD,
    MAX_TEMPLATE_HANDWRITING_BOX_AREA,
    MAX_TOTAL_TEMPLATE_HANDWRITING_AREA,
    MAX_ANSWER_REGION_AREA,
    MAX_TOTAL_ANSWER_REGION_AREA,
    MAX_ANSWER_REGION_OVERLAP_RATIO,
    _QUESTION_TYPES,
    _is_int,
    _question_number,
    normalize_answer_regions,
    validate_drawing_schema_geometry,
    normalize_question_schema,
    _slot_geometry_metadata,
    _bind_slot_geometry,
    _has_text_slot_labels,
    _valid_drawing_coordinates,
    _validate_printed_solve_anchors,
    _normalize_template_confidence,
    _normalize_template_provenance,
    _template_item_is_human_confirmed,
    _template_confidence_is_trusted,
    template_confidence_diagnostics,
    validate_template_confidence_gate,
    _trusted_template_handwriting_regions,
    _validated_template_handwriting_regions,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
)
from services.template_evidence_canonical import (
    CANONICALIZATION_VERSION,
    CANONICAL_COORDINATE_SYSTEM,
    CANONICAL_SOURCE_COORDINATE_SYSTEM,
    CANONICAL_PAGE_LONG_EDGE,
    CANONICAL_PAGE_MARGIN,
    _STORAGE_NAMESPACE_RE,
    _CANONICAL_CANDIDATE_KEYS,
    CanonicalTemplate,
    _LoadedTemplateSource,
    _flatten_to_rgb,
    _resize_long_edge,
    _validated_source_dimensions,
    _load_rgb_image,
    _load_template_source,
    _order_page_quad,
    _page_content_metrics,
    _full_frame_page_confidence,
    _full_frame_content_confidence,
    _approximate_quad,
    _detect_page_quad,
    _canonical_page,
    _sha256_file,
    _save_rgb_png_atomic,
    _copy_file_atomic,
    parse_canonical_metadata,
    accept_canonical_template_candidate,
    canonicalize_template_image,
    _safe_source_image_path,
    _validate_canonicalization,
)
from services.template_evidence_alignment import (
    LOCAL_TILES_X,
    LOCAL_MIN_INLIERS,
    LOCAL_MIN_INLIER_RATIO,
    LOCAL_OVERLAP_RATIO,
    LOCAL_MAX_TRANSLATION_RATIO,
    LOCAL_MAX_SCALE_CHANGE,
    LOCAL_MAX_ROTATION_DEGREES,
    LOCAL_MAX_SHEAR,
    _coverage_for_homography,
    _trusted_local_affine,
    _refine_local_alignment,
    _refine_source_mask,
)
from services.template_evidence_crops import (
    _answer_region_source_coverage,
    _answer_crop_boxes,
    _answer_crop_source_coverage,
    _frozen_column_bounds,
    _resolved_frozen_pixel_bounds,
    _pixel_regions,
    _horizontal_overlap,
    _same_answer_lane,
    _answer_crops,
)
from services.template_evidence_residue import (
    TEMPLATE_INK_LOCAL_CONTRAST,
    TEMPLATE_INK_MAX_GRAY,
    TEMPLATE_EDGE_DILATION_SIZE,
    TEMPLATE_ILLUMINATION_SIGMA,
    TEMPLATE_ILLUMINATION_MAX_OFFSET,
    MIN_NEW_STROKE_SUPPORT_PIXELS,
    MIN_NEW_STROKE_SUPPORT_RATIO,
    FILL_LINE_OPENING_WIDTH,
    FILL_LINE_VERTICAL_DILATION,
    FILL_LINE_SUPPORT_PAD_X,
    FILL_LINE_SUPPORT_PAD_Y,
    FILL_LINE_CROSSING_HORIZONTAL_PAD,
    FILL_LINE_MIN_SIDE_SUPPORT_PIXELS,
    TEMPLATE_HANDWRITING_MIN_AREA,
    TEMPLATE_HANDWRITING_RECOVERY_PAD,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_PIXELS,
    TEMPLATE_HANDWRITING_RECOVERY_STRONG_SEED_RATIO,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_AREA,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_RATIO,
    TEMPLATE_HANDWRITING_RECOVERY_MAX_EXTENT,
    TEMPLATE_HANDWRITING_RECOVERY_MIN_ELONGATION,
    TEMPLATE_HANDWRITING_RECOVERY_MAX_PAGE_SPAN_RATIO,
    _template_handwriting_region_mask,
    _merge_template_exemption_regions,
    _erase_template_handwriting,
    _suppress_registered_template_residue_v2,
    _restore_fill_line_overlap_strokes,
    _local_ink_mask,
    _illumination_normalized_student_gray,
)
from services.template_evidence_pixels import (
    TEMPLATE_OVERLAP_DARKER_MARGIN,
    _answer_only_pixels_v2,
    _choice_answer_pixels,
    _new_ink_pixel_count,
    _confident_blank_questions,
)
from services.template_evidence_artifacts import (
    ARTIFACT_VERSION,
    PRINTED_SOLVE_LABEL_POLICY,
    build_template_artifact,
    _safe_artifact_path,
    _erase_template_handwriting_rgb,
    materialize_template_difference_artifact,
    _verified_template_manifest,
    verify_template_artifacts,
)
from services.template_evidence_sheets import (
    FOCUS_EVIDENCE_LABEL_WIDTH,
    FOCUS_EVIDENCE_PADDING,
    FOCUS_EVIDENCE_MAX_IMAGE_PIXELS,
    FOCUS_EVIDENCE_MAX_TOTAL_PIXELS,
    _machine_label,
    _build_evidence_table,
    _anchor_block_crops,
    build_focus_evidence_sheet_layout,
    build_multi_evidence_from_aligned,
)
from services.template_evidence_aligned import (
    build_evidence_from_aligned,
)
from services.template_evidence_labels import (
    EVIDENCE_Q_LABEL_COLOR,
    EVIDENCE_Q_LABEL_FONT_SIZE,
    EVIDENCE_Q_LABEL_PADDING_X,
    EVIDENCE_Q_LABEL_PADDING_Y,
    EVIDENCE_Q_LABEL_GAP,
    _evidence_question_label_font,
    _drawing_region_pixels,
    _render_column_question_labels,
    validate_drawing_label_geometry,
    _new_ink_extent,
    _unmarked_zones,
    _route_unmarked_answer_positions,
)
from services.template_evidence_columns import (
    _FullColumnLayout,
    _ColumnLabelSets,
    _snapshot_page_type_id,
    _require_aligned_pixels,
    _layout_question_sequence,
    _layout_question_kinds,
    _full_column_layout,
    _full_column_answer_pixels,
    _route_layout_answer_positions,
    _expected_column_labels,
    _FROZEN_COLUMN_EVIDENCE_KEYS,
    _render_full_columns,
    _verify_rendered_labels,
    _FROZEN_COLUMN_DIAGNOSTIC_KEYS,
    _full_column_diagnostics,
    build_full_column_difference_evidence_from_aligned,
)
from services.template_evidence_matcher import (
    FEATURE_MAX_EDGE,
    FEATURE_NFEATURES,
    MIN_GOOD_MATCHES,
    MIN_INLIERS,
    MIN_INLIER_RATIO,
    MIN_PAGE_COVERAGE,
    MAX_REPROJECTION_RMSE,
    LOWE_RATIO,
    _EVIDENCE_NAME_RE,
    _TemplateFeatures,
    _feature_gray_and_scales,
    _keypoint_points,
    _candidate_public,
    _build_question_anchor_overlay,
    TemplateEvidenceMatcher,
    cleanup_evidence,
)
