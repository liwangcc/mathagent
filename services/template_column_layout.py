"""Qwen Flash layout adapter for teacher-template one-to-three-column columns.

2026-10-03 起分栏由 qwen3.8-flash（DashScope 按量）承担，思考按 Qwen 官方
关闭组合固定关闭（enable_thinking=false + reasoning_effort="none" 两键同发）。
换用该模型时，公开布局草稿/坐标/修订合同未变。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from config import (
    DASHSCOPE_GLOBAL_CONCURRENCY,
    LLM_KEY_CONCURRENCY,  # compatibility export for existing diagnostics/tests
    MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS,
    TEMPLATE_CUT_API_KEY,
    TEMPLATE_CUT_API_KEYS,
    TEMPLATE_CUT_API_KEY_WEIGHTS,
    TEMPLATE_CUT_BASE_URL,
    TEMPLATE_CUT_ENABLE_THINKING,
    TEMPLATE_CUT_KEY_CONCURRENCY,
    TEMPLATE_CUT_MAX_TOKENS,
    TEMPLATE_CUT_MODEL,
    TEMPLATE_CUT_REASONING_EFFORT,
    TEMPLATE_CUT_TEMPERATURE,
    TEMPLATE_CUT_TIMEOUT_S,
)
from services import dashscope_budget
from services.key_pool import (
    KeyPool,
    acquire_key,
    mark_shared_credential_cooldown,
    refresh_shared_credential_health,
    shared_credential_cooldown_seconds,
)
from services.model_concurrency import (
    RateBudgetTimeout,
    configured_fair_share_policy,
    get_model_limiter,
)
from services.retry_backoff import (
    exponential_retry_delay,
    parse_retry_after_seconds,
)
from services.template_column_layout_contract import (
    LAYOUT_CONTRACT_VERSION,
    LAYOUT_COORDINATE_SPACE,
    PIXEL_ENDPOINT_VERSION,
    DEFAULT_COLUMN_MODE,
    _COLUMN_MODES,
    _COLUMN_MODE_ALIASES,
    MIN_COLUMN_WIDTH_1000,
    TemplateCutError,
    normalize_column_mode,
    _normalize_column_mode,
    normalize_layout_mode,
    _normalize_layout_mode,
    _as_exact_int,
    _layout_columns_from_value,
    validate_layout_boundaries,
    _numeric_flag,
    _box_crosses_boundary,
    _line_crosses_boundary,
    _validate_crossing_evidence,
    _validate_layout_boundaries,
    validate_three_column_proposal,
    _validate_three_column_proposal,
    _payload_from_columns,
    _build_column_payload,
    _integer_json_number,
)
from services.template_column_layout_image import (
    _MAX_IMAGE_BYTES,
    _sha256_file,
    _validate_template_image,
    _estimated_template_payload_bytes,
    _encode_template_data_url,
)
from services.template_column_layout_provider import (
    _PROVIDER,
    _TEMPLATE_CAPABILITY,
    _CAPABILITY_FAILURE_COOLDOWN_S,
    _template_pool,
    _template_pool_signature,
    _template_capability_unavailable_until,
    _cached_template_key_pool,
    _template_cut_route,
    _resolve_template_key_pool,
    _local_template_capability_failed,
    _shared_template_capability_failed,
    _mark_template_capability_failure,
    _clear_template_capability_failure,
    get_column_layout_readiness,
    _post_with_shared_lease,
    _acquire_template_rate_budget,
)
from services.template_column_layout_prompts import (
    _DOUBLE_LAYOUT_EXAMPLE,
    _TRIPLE_LAYOUT_EXAMPLE,
    _PROMPT,
    _TRIPLE_PROMPT,
    _DOUBLE_PROMPT,
    _extract_json_text,
    _strict_payload,
)
from services.template_column_layout_local import (
    _LOCAL_LAYOUT_MAX_WIDTH,
    _LOCAL_LAYOUT_MIN_SOURCE_WIDTH,
    _DOUBLE_EVIDENCE_VERSION,
    _DOUBLE_EVIDENCE_THRESHOLDS,
    _TRIPLE_EVIDENCE_VERSION,
    _TRIPLE_EVIDENCE_THRESHOLDS,
    _infer_local_two_column_payload,
    _inspect_local_double_evidence,
    _inspect_local_triple_evidence,
    _double_layout_draft,
)
from services.template_column_layout_drafts import (
    _OPERATION,
    _coerce_revision,
    _layout_signature,
    _next_layout_revision,
    _enrich_layout_columns,
    _build_layout_result,
    _review_layout_result,
    revise_layout_draft,
    layout_needs_review,
    whole_page_fallback_layout,
    update_layout_draft,
    edit_layout_draft,
)
from services.template_column_layout_recognize import (
    _MAX_TRANSIENT_ATTEMPTS,
    _RETRY_DELAY_S,
    _RETRY_MAX_DELAY_S,
    _POOL_FAILOVER_DELAY_S,
    _KEY_ACQUIRE_TIMEOUT_S,
    _retry_after_seconds,
    recognize_column_layout,
)

logger = logging.getLogger(__name__)
