"""Lease-based, fail-closed orchestration for opportunistic preprocessing nodes.

The code lives in one module per part of running a job; each imports only the
parts above it:

    accelerator_jobs_base        configuration checked at import, stage names, time and hashing helpers
    accelerator_jobs_identity    a job's input identity: template OCR evidence, vision model layout
    accelerator_jobs_nodes       nodes: registration, credentials, heartbeat, capabilities, health
    accelerator_jobs_queue       jobs entering, re-entering and timing out of the queue
    accelerator_jobs_leases      the next job a node may lease, a live lease, its assets and uploads
    accelerator_jobs_results     what a submitted result must look like, per stage
    accelerator_jobs_completion  a job's result or failure, server validation, the public view

This module re-exports every name, so ``from services.accelerator_jobs import
...`` keeps working for callers and tests; new code may import from the module
that defines a name.
"""
from __future__ import annotations

# Kept importable: configuration, stage names and helpers (accelerator_jobs_base.py).
from services.accelerator_jobs_base import (  # noqa: F401
    ALGORITHM_VERSION,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    NODE_STALE_SECONDS,
    JOB_DEADLINE_SECONDS,
    NODE_MAX_ACTIVE_LEASES,
    CPU_FALLBACK_STALE_SECONDS,
    TEMPLATE_OCR_EVIDENCE_STAGE,
    STUDENT_MULTI_EVIDENCE_STAGE,
    VISION_MODEL_STAGE,
    VISION_MODEL_REQUEST_CONTRACT_VERSION,
    VISION_MODEL_RESPONSE_CONTRACT_VERSION,
    utcnow,
    canonical_json,
    sha256_file,
    sha256_json,
)
# Kept importable: a job's input identity (accelerator_jobs_identity.py).
from services.accelerator_jobs_identity import (  # noqa: F401
    TEMPLATE_OCR_IDENTITY_VERSION,
    TEMPLATE_OCR_CACHE_DIR_NAME,
    _IDENTIFIER_RE,
    _SHA256_RE,
    _TEMPLATE_OCR_IDENTITY_RE,
    _MAX_MODEL_PROMPT_BYTES,
    _MAX_MODEL_ASSETS,
    _MODEL_PROVIDER_PROFILES,
    normalize_template_ocr_question_schema,
    template_ocr_identity_payload,
    template_ocr_identity,
    template_ocr_cache_root,
    template_ocr_cache_path,
    template_ocr_layout_identity,
    _is_sha256,
    _normalize_vision_model_layout,
)
# Kept importable: nodes (accelerator_jobs_nodes.py).
from services.accelerator_jobs_nodes import (  # noqa: F401
    _STAGE_ARTIFACT_CONTRACTS,
    _SUPPORTED_ARTIFACT_CONTRACTS,
    _ALLOWED_STAGES,
    _ALLOWED_LANES,
    _hash_secret,
    _node_capabilities,
    node_hardware_ready,
    _node_health,
    _node_public,
    register_node,
    _node_job_lanes,
    _node_can_access_job,
    node_supported_stages,
    node_supports_stage,
    node_supported_artifact_contracts,
    node_supports_artifact_contract,
    _node_slot_geometry_match,
    validate_node_slot_geometry,
    node_leaseable_stages,
    node_supported_provider_profiles,
    node_supported_provider_models,
    list_nodes,
    list_teacher_desktop_nodes,
    _requeue_node_leases,
    rotate_node_token,
    revoke_node,
    authenticate_node,
    heartbeat,
)
# Kept importable: the queue (accelerator_jobs_queue.py).
from services.accelerator_jobs_queue import (  # noqa: F401
    _OFFLOAD_ONLY_STAGES,
    _RETRYABLE_OFFLOAD_ERROR_CODES,
    _RETRYABLE_VISION_RESUME_ERROR_CODES,
    _CONTRACT_TERMINAL_STATUSES,
    enqueue_job,
    is_retryable_offload_terminal,
    resume_offload_job,
    _fallback_job,
    expire_leases,
)
# Kept importable: leases (accelerator_jobs_leases.py).
from services.accelerator_jobs_leases import (  # noqa: F401
    _IMAGE_PROCESSING_STAGES,
    _scope_filters,
    _node_lease_capacity,
    _vision_provider_match,
    _node_stage_slot_groups,
    _platform_fair_teacher_id,
    lease_next_job,
    _lease_matches,
    _owned_live_lease,
    renew_lease,
    verify_job_asset,
    allowed_artifact_kinds_for_stage,
    authorize_candidate_artifact_upload,
)
# Kept importable: result validation (accelerator_jobs_results.py).
from services.accelerator_jobs_results import (  # noqa: F401
    _ALLOWED_RESULT_KEYS,
    _REQUIRED_RESULT_KEYS,
    _CLIENT_EVIDENCE_RESULT_KEYS,
    _CANONICAL_RESULT_KEYS,
    _TEMPLATE_OCR_EVIDENCE_RESULT_KEYS,
    _TEMPLATE_OCR_EVIDENCE_INPUT_IDENTITY_KEYS,
    _EVIDENCE_DIAGNOSTIC_KEYS,
    _OPTIONAL_EVIDENCE_DIAGNOSTIC_KEYS,
    _COVERAGE_ITEM_KEYS,
    _MAX_RESULT_QUESTION_COUNT,
    _MAX_RESULT_QUESTION_LENGTH,
    _MAX_EVIDENCE_DIAGNOSTICS_BYTES,
    _MAX_MODEL_OUTPUT_BYTES,
    _normalize_result_sequence,
    _normalize_evidence_diagnostics,
    _validate_canonical_candidate_payload,
    _validate_template_ocr_evidence_candidate_payload,
    _validate_vision_model_result_payload,
    _validate_candidate_payload,
)
# Kept importable: finishing a job (accelerator_jobs_completion.py).
from services.accelerator_jobs_completion import (  # noqa: F401
    submit_result,
    begin_server_validation,
    finish_server_validation,
    fail_job,
    job_public,
)
# Kept importable: everything this module imported before the split;
# callers and tests read some of these names through it.
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from sqlalchemy import and_, case, exists, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from models_accelerator import AcceleratorJob, AcceleratorNode
from services.accelerator_artifacts import (
    CLIENT_EVIDENCE_CONTRACT_VERSION,
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
    CLIENT_EVIDENCE_SLOT_GEOMETRY_CONTRACT_VERSION,
    layout_requires_slot_geometry,
    validate_slot_geometry_receipt,
    TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
    TEMPLATE_OCR_EVIDENCE_KIND,
    candidate_artifact_path,
    cleanup_candidate_artifacts,
    normalize_homography,
    validate_artifact_manifest,
    validate_uploaded_artifact_file,
)
from services.template_canonicalization_contract import (
    CANONICALIZATION_STAGE,
    CANONICAL_TEMPLATE_CONTRACT_VERSION,
    validate_canonicalization_metadata,
)

logger = logging.getLogger(__name__)
