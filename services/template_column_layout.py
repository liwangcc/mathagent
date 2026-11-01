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

# qwen3.8-flash 与批改/转录共享 DashScope 按量账号，限流与凭证健康按
# provider="dashscope" 同域协调。
_PROVIDER = "dashscope"
_TEMPLATE_CAPABILITY = f"{_PROVIDER}:template_vision"
_OPERATION = "RecognizeColumnLayout"
# One initial request plus one retry on the same subscription route. Credential
# and request-contract errors fail immediately and never cross to payg.
_MAX_TRANSIENT_ATTEMPTS = 2
_RETRY_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 5.0
_POOL_FAILOVER_DELAY_S = 0.05
_KEY_ACQUIRE_TIMEOUT_S = float(MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS)
_CAPABILITY_FAILURE_COOLDOWN_S = 60.0
_LOCAL_LAYOUT_MAX_WIDTH = 1200
_LOCAL_LAYOUT_MIN_SOURCE_WIDTH = 800
# Layout values are part of the public template draft contract.  Keep the
# contract independent from the provider/model names so a teacher can edit a
# draft without making another model request.
LAYOUT_CONTRACT_VERSION = "template-column-layout-v2"
LAYOUT_COORDINATE_SPACE = "normalized_1000_v1"
PIXEL_ENDPOINT_VERSION = "rounded_source_pixels_v1"
DEFAULT_COLUMN_MODE = "double"
_COLUMN_MODES = frozenset({"single", "double", "triple"})
_COLUMN_MODE_ALIASES = {
    "单栏": "single",
    "双栏": "double",
    "三栏": "triple",
    "one": "single",
    "two": "double",
    "three": "triple",
}
# A 1000-unit column narrower than this is not useful for question OCR.  The
# value is deliberately conservative: ordinary two/three-column pages are
# well above it, while malformed model proposals fail closed.
MIN_COLUMN_WIDTH_1000 = 80
# Double-column gate scoring rules.  Bump the version whenever a threshold or
# the reason vocabulary changes; the values are copied into every evidence
# record so a persisted decision stays auditable against its own rules.
_DOUBLE_EVIDENCE_VERSION = "double-gutter-evidence-v4"
_DOUBLE_EVIDENCE_THRESHOLDS: dict[str, float | int] = {
    # Ink fraction in the central band that reads as a printed seam/content.
    "vertical_peak_density_max": 0.55,
    # Rows with ink through the gutter, and the seam ink relative to the sides.
    "seam_active_rows_max": 0.55,
    "seam_density_floor": 0.025,
    "seam_to_side_ratio_max": 0.30,
    # Fraction of body rows containing one long run crossing the gutter.
    "cross_line_fraction_max": 0.002,
    # Two-sided content requirements.
    "side_density_min": 0.012,
    "side_active_rows_min": 0.28,
    "left_right_ratio_max": 3.0,
    # Canonicalization can thicken a page-bottom border into several dark
    # rows.  It is outside the question body and must not look like a printed
    # question crossing the gutter.  Other density checks still inspect the
    # full body down to 95% of the page.
    "cross_line_scan_bottom_fraction": 0.90,
    # Permitted distance of the seam from page centre, in 1000 units.
    "center_offset_1000_max": 35,
    "broad_valley_1000_min": 400,
    "broad_valley_1000_max": 600,
    "broad_valley_density_max": 0.02,
    # Column width floor shared with boundary validation.
    "min_column_width_1000": MIN_COLUMN_WIDTH_1000,
}
# Triple-column content gate.  The layout model proposes the seams; this check
# requires every column to carry printed content and every seam to be a blank
# valley before the proposal may freeze crops.
_TRIPLE_EVIDENCE_VERSION = "triple-content-evidence-v1"
_TRIPLE_EVIDENCE_THRESHOLDS: dict[str, float | int] = {
    "column_density_min": 0.008,
    "column_active_rows_min": 0.20,
    "seam_density_max": 0.030,
    "seam_to_side_ratio_max": 0.45,
    "cross_line_fraction_max": 0.002,
    "min_column_width_1000": MIN_COLUMN_WIDTH_1000,
}
_template_pool: KeyPool | None = None
_template_pool_signature = ""
_template_capability_unavailable_until = 0.0
logger = logging.getLogger(__name__)
_DOUBLE_LAYOUT_EXAMPLE = (
    '{"column_count":2,"columns":[{"column_index":1,"x1_1000":0,"x2_1000":500},'
    '{"column_index":2,"x1_1000":500,"x2_1000":1000}]}'
)
_TRIPLE_LAYOUT_EXAMPLE = (
    '{"column_count":3,"columns":[{"column_index":1,"x1_1000":0,"x2_1000":333},'
    '{"column_index":2,"x1_1000":333,"x2_1000":667},'
    '{"column_index":3,"x1_1000":667,"x2_1000":1000}]}'
)
_PROMPT = (
    """你是试卷模板版面分栏模型。
输入是一张已经完成旋转、透视和页面边界矫正的完整试卷模板。只按服务端提供的栏数要求确定分界；如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏。不要识别题干、答案或学生笔迹。
服务端指定的栏数是硬约束，返回的 column_count 必须与其一致；页眉或标题跨栏不算一栏。分界线不得穿过题目或作答区域。
这个分栏结果会由系统冻结，并复用于同一页型的所有学生试卷，所以必须依据当前图片实际排版，不能按固定的 500/333 像素猜测。
坐标为整图横向 0..1000。各栏必须从左到右连续、无空隙、无重叠，并完整覆盖 0..1000。
只返回 JSON，不要解释或 Markdown：
"""
    + _DOUBLE_LAYOUT_EXAMPLE
)

# An explicit triple choice must not leave a two-column example or a generic
# "choose the minimum" branch for the model to resolve.  Derive the fixed
# contract from the compatible general prompt so future common wording stays
# aligned without duplicating the full instruction block.
_TRIPLE_PROMPT = (
    _PROMPT
    .replace(
        "如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏",
        "教师已经选择三栏；不要自行选择栏数，必须返回恰好三栏和两条实际边界",
    )
    .replace(_DOUBLE_LAYOUT_EXAMPLE, _TRIPLE_LAYOUT_EXAMPLE)
)

# An explicit double choice pins the count the same way the triple contract
# does, so the model cannot drift into an invented column count.
_DOUBLE_PROMPT = (
    _PROMPT
    .replace(
        "如果服务端没有指定栏数，才判断整页需要单栏、双栏还是三栏",
        "教师已经选择双栏；不要自行选择栏数，必须返回恰好两栏和一条实际边界",
    )
)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class TemplateCutError(RuntimeError):
    """Safe, provider-neutral error for template layout recognition."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def normalize_column_mode(value: object = None, *, default: str = DEFAULT_COLUMN_MODE) -> str:
    """Normalize the teacher-facing single/double/triple layout choice.

    ``None``/an empty value means the documented default (double).  Numeric
    counts are intentionally not accepted: accepting ``2`` here makes it too
    easy for a stale caller to silently select a different layout contract.
    Chinese labels are accepted as a UI convenience but are normalized before
    they reach the layout implementation.
    """
    fallback = str(default or DEFAULT_COLUMN_MODE).strip().lower()
    fallback = _COLUMN_MODE_ALIASES.get(fallback, fallback)
    if fallback not in _COLUMN_MODES:
        raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式默认值无效")
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    if not isinstance(value, str):
        raise TemplateCutError(
            "LAYOUT_MODE_INVALID",
            "分栏模式必须是 single、double 或 triple",
        )
    normalized = value.strip().lower()
    normalized = _COLUMN_MODE_ALIASES.get(normalized, normalized)
    if normalized not in _COLUMN_MODES:
        raise TemplateCutError(
            "LAYOUT_MODE_INVALID",
            "分栏模式必须是 single、double 或 triple",
        )
    return normalized


# Keep private/alternate spellings available for callers that were written
# against an earlier draft of the handoff contract.
_normalize_column_mode = normalize_column_mode
normalize_layout_mode = normalize_column_mode
_normalize_layout_mode = normalize_column_mode


def _as_exact_int(value: object) -> int | None:
    """Return an integer for JSON integer values (including exact floats)."""
    return _integer_json_number(value)


def _layout_columns_from_value(
    value: object,
    *,
    allow_implicit_index: bool = True,
) -> list[dict[str, int]]:
    """Parse an ordered list of normalized column boundaries.

    The parser is intentionally stricter than the downstream legacy crop
    parser: every column must be indexed, contiguous, and cover the full page.
    It accepts either the provider's ``columns`` list or a list of normalized
    boundary values (``[0, 500, 1000]``), which is useful for teacher drafts.
    """
    if isinstance(value, dict):
        value = value.get("columns")
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")

    # A compact boundary list is convenient for confirmation-page edits.
    if value and all(not isinstance(item, dict) for item in value):
        bounds = [_as_exact_int(item) for item in value]
        if any(item is None for item in bounds) or len(bounds) != len(value):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须是整数")
        if len(bounds) < 1 or len(bounds) > 4:
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")
        # Accept either all endpoints (0..1000 included) or only the interior
        # seam coordinates supplied by a confirmation control.
        if bounds[0] != 0:
            bounds = [0, *bounds]
        if bounds[-1] != 1000:
            bounds = [*bounds, 1000]
        if len(bounds) < 2 or len(bounds) > 4:
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界数量无效")
        value = [
            {"column_index": index, "x1_1000": left, "x2_1000": right}
            for index, (left, right) in enumerate(zip(bounds, bounds[1:]), 1)
        ]

    columns: list[dict[str, int]] = []
    previous_right = 0
    for position, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏结构无效")
        index_value = raw.get("column_index")
        if index_value is None and allow_implicit_index:
            index_value = position
        index = _as_exact_int(index_value)
        left = _as_exact_int(raw.get("x1_1000"))
        right = _as_exact_int(raw.get("x2_1000"))
        if (
            index != position
            or left is None
            or right is None
            or left != previous_right
            or not 0 <= left < right <= 1000
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须连续覆盖 0..1000")
        if right - left < MIN_COLUMN_WIDTH_1000:
            raise TemplateCutError(
                "LAYOUT_BOUNDARY_INVALID",
                "分栏宽度过窄，无法安全进入 OCR",
            )
        columns.append({
            "column_index": int(index),
            "x1_1000": int(left),
            "x2_1000": int(right),
        })
        previous_right = int(right)
    if previous_right != 1000:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏边界必须覆盖整页")
    return columns


def validate_layout_boundaries(
    value: object,
    *,
    column_mode: object = None,
    layout_mode: object = None,
    column_count: object = None,
    mode: object = None,
    requested_mode: object = None,
    min_column_width_1000: int = MIN_COLUMN_WIDTH_1000,
    evidence: object = None,
) -> list[dict[str, int]]:
    """Validate a model/teacher boundary proposal and return canonical bounds.

    Triple proposals are required to contain exactly three columns.  Geometry
    or explicit evidence that a question/answer region or printed line crosses
    a proposed seam is rejected; the caller can then expose a ``needs_review``
    draft instead of sending unsafe crops downstream.
    """
    supplied_modes = [
        item
        for item in (column_mode, layout_mode, column_count, mode, requested_mode)
        if item is not None
    ]
    if len(supplied_modes) > 1:
        normalized_modes = [normalize_column_mode(item) for item in supplied_modes]
        if len(set(normalized_modes)) != 1:
            raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式参数冲突")
        mode = normalized_modes[0]
    else:
        mode = normalize_column_mode(
            supplied_modes[0] if supplied_modes else None,
            default=DEFAULT_COLUMN_MODE,
        )
    # Use the module threshold unless a stricter caller supplied one.
    try:
        minimum = int(min_column_width_1000)
    except (TypeError, ValueError) as exc:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "最小栏宽无效") from exc
    if minimum < 1 or minimum > 500:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "最小栏宽无效")
    # Temporarily parse with the public threshold, then apply a caller's
    # stricter threshold without mutating the input.
    columns = _layout_columns_from_value(value)
    if mode == "single" and len(columns) != 1:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "单栏必须覆盖整页")
    if mode == "double" and len(columns) != 2:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "双栏必须包含一个中缝")
    if mode == "triple" and len(columns) != 3:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "三栏必须包含两条边界")
    if any(item["x2_1000"] - item["x1_1000"] < minimum for item in columns):
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "分栏宽度过窄，无法安全进入 OCR")
    _validate_crossing_evidence(columns, evidence)
    return columns


def _numeric_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "cross", "crossing"}
    return bool(value)


def _box_crosses_boundary(box: object, boundary: int) -> bool:
    if isinstance(box, dict):
        left = box.get("x1_1000", box.get("left", box.get("x1")))
        right = box.get("x2_1000", box.get("right", box.get("x2")))
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        left, right = box[0], box[2]
    else:
        return False
    left = _as_exact_int(left)
    right = _as_exact_int(right)
    return left is not None and right is not None and left < boundary < right


def _line_crosses_boundary(line: object, boundary: int) -> bool:
    if isinstance(line, dict):
        # Explicit flags are authoritative when supplied by a detector/model.
        for key in ("crosses_boundary", "crossing", "cross_column", "spans_gutter"):
            if key in line and _numeric_flag(line[key]):
                return True
        x = line.get("x1000", line.get("x", line.get("center_x1000")))
        if x is not None:
            x = _as_exact_int(x)
            if x is not None and abs(x - boundary) <= 2:
                return True
        left = line.get("x1_1000", line.get("left", line.get("x1")))
        right = line.get("x2_1000", line.get("right", line.get("x2")))
    elif isinstance(line, (list, tuple)) and len(line) >= 4:
        left, right = line[0], line[2]
    else:
        return False
    left = _as_exact_int(left)
    right = _as_exact_int(right)
    return left is not None and right is not None and left <= boundary <= right


def _validate_crossing_evidence(columns: list[dict[str, int]], evidence: object) -> None:
    if not isinstance(evidence, dict):
        return
    boundaries = [item["x2_1000"] for item in columns[:-1]]
    for key in (
        "cross_column",
        "cross_column_question",
        "cross_column_questions",
        "question_crossings",
        "answer_crossings",
        "boundary_crossings",
        "has_cross_column_question",
        "has_crossing_line",
        "printed_line_crossing",
        "line_crossings",
        "crossing_boundary_lines",
        "cross_column_items",
        "question_spans",
        "answer_spans",
    ):
        value = evidence.get(key)
        if _numeric_flag(value) or (isinstance(value, (list, tuple)) and bool(value)):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "题目或印刷线跨越分界线")
    for key in (
        "question_boxes",
        "question_regions",
        "answer_boxes",
        "answer_regions",
        "items",
    ):
        boxes = evidence.get(key)
        if isinstance(boxes, dict):
            boxes = [boxes]
        if isinstance(boxes, (list, tuple)) and any(
            _box_crosses_boundary(box, boundary)
            for box in boxes
            for boundary in boundaries
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "题目或作答区跨越分界线")
    for key in ("lines", "vertical_lines", "printed_lines", "boundary_lines", "crossing_lines"):
        lines = evidence.get(key)
        if isinstance(lines, dict):
            lines = [lines]
        if isinstance(lines, (list, tuple)) and any(
            _line_crosses_boundary(line, boundary)
            for line in lines
            for boundary in boundaries
        ):
            raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "贯穿分界线的印刷线不能作为中缝")


_validate_layout_boundaries = validate_layout_boundaries


def validate_three_column_proposal(value: object, **kwargs: object) -> list[dict[str, int]]:
    kwargs.pop("column_mode", None)
    kwargs.pop("layout_mode", None)
    kwargs.pop("column_count", None)
    return validate_layout_boundaries(value, column_mode="triple", **kwargs)


_validate_three_column_proposal = validate_three_column_proposal


def _payload_from_columns(columns: list[dict[str, int]], *, source_key: str, trust: str) -> dict[str, Any]:
    normalized = [dict(item) for item in columns]
    candidates: list[dict[str, Any]] = []
    for candidate_index, column in enumerate(normalized):
        bbox = [column["x1_1000"], 0, column["x2_1000"], 1000]
        candidates.append({
            "candidate_index": candidate_index,
            "source_key": source_key,
            "candidate_role": "question_block",
            "provider_ids": [f"col_{column['column_index']}"],
            "text": "",
            "question_number_hint": "",
            "question_number_hint_source": "",
            "coordinate_trust": trust,
            "usable_for_original_overlay": True,
            "raw_sha256": hashlib.sha256(
                json.dumps(column, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "template_bbox_1000": bbox,
        })
    return {
        "column_layout": {
            "column_count": len(normalized),
            "columns": normalized,
        },
        "candidates": candidates,
    }


_build_column_payload = _payload_from_columns


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_template_image(image_path: str | os.PathLike[str]) -> dict[str, Any]:
    from PIL import Image

    path = Path(image_path)
    if not path.is_file():
        raise TemplateCutError("IMAGE_NOT_FOUND", "教师模板图片不存在")
    size_bytes = path.stat().st_size
    if size_bytes <= 0 or size_bytes >= _MAX_IMAGE_BYTES:
        raise TemplateCutError("IMAGE_SIZE_INVALID", "教师模板图片必须小于 10MB")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = str(image.format or "").lower()
    except (OSError, ValueError) as exc:
        raise TemplateCutError("IMAGE_INVALID", "教师模板图片无法解码") from exc
    if not (15 < width < 8192 and 15 < height < 8192):
        raise TemplateCutError(
            "IMAGE_DIMENSIONS_INVALID",
            "教师模板图片边长必须在 15 到 8192 像素之间",
        )
    if max(width, height) / float(min(width, height)) >= 50:
        raise TemplateCutError(
            "IMAGE_ASPECT_INVALID",
            "教师模板图片长宽比必须小于 50",
        )
    image_format = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}.get(image_format, "")
    if not image_format:
        raise TemplateCutError(
            "IMAGE_TYPE_INVALID",
            "模板识别仅接受 JPG 或 PNG 图片",
        )
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "image_format": image_format,
    }


def _estimated_template_payload_bytes(raw_bytes: int) -> int:
    """Estimate raw, Base64, JSON text, and serialized HTTP body residency."""
    if raw_bytes < 1:
        raise ValueError("template image payload is empty")
    encoded_bytes = 4 * ((int(raw_bytes) + 2) // 3)
    return int(raw_bytes) + 3 * encoded_bytes


def _encode_template_data_url(image_path: str, mime: str) -> str:
    image_bytes = Path(image_path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _cached_template_key_pool(
    keys: list[str],
    base_urls: list[str],
    weights: list[int] | None = None,
) -> KeyPool:
    """Build a pool only from the dedicated pay-as-you-go DashScope keys."""
    global _template_pool, _template_pool_signature
    signature = hashlib.sha256(
        "\0".join(
            (
                "template-subscription",
                *keys,
                *base_urls,
                *(str(weight) for weight in (weights or [])),
                str(TEMPLATE_CUT_KEY_CONCURRENCY),
            )
        ).encode("utf-8")
    ).hexdigest()
    if _template_pool is None or signature != _template_pool_signature:
        _template_pool = KeyPool(
            keys,
            max_concurrent_per_key=TEMPLATE_CUT_KEY_CONCURRENCY,
            weights=weights,
            base_urls=base_urls,
        )
        _template_pool_signature = signature
    return _template_pool


def _template_cut_route():
    """Return the pay-as-you-go DashScope route that owns column layout.

    qwen3.8-flash 一律走按量；没有回退到 MiMo 订阅、通用 LLM_* 或批改凭据。
    """
    keys = tuple(item.strip() for item in str(TEMPLATE_CUT_API_KEYS or "").split(",") if item.strip())
    single = str(TEMPLATE_CUT_API_KEY or "").strip()
    if single and single not in keys:
        keys = (single, *keys)
    weights = tuple(
        int(item.strip())
        for item in str(TEMPLATE_CUT_API_KEY_WEIGHTS or "").split(",")
        if item.strip().isdigit() and int(item.strip()) > 0
    )
    # 设置页「模板定位与解题」组的配置优先。只有确实写了组级值时才覆盖，
    # 这样多 Key 号池（TEMPLATE_CUT_API_KEYS）与环境变量部署完全不受影响。
    profile_base_url = ""
    profile_model = ""
    try:
        from services.model_profiles import resolve as _resolve_profile

        profile = _resolve_profile("template_solve")
        if profile.get("source") == "group" and profile.get("api_key"):
            single = profile["api_key"]
            keys = (single,)
            weights = ()
            profile_base_url = profile.get("base_url") or ""
            profile_model = profile.get("model") or ""
    except Exception:  # pragma: no cover - 设置存储不可用时保持原行为
        profile_base_url = ""
        profile_model = ""
    return SimpleNamespace(
        base_url=str(profile_base_url or TEMPLATE_CUT_BASE_URL or "").strip(),
        api_key=single,
        api_keys=keys,
        api_key_weights=weights,
        key_concurrency=int(TEMPLATE_CUT_KEY_CONCURRENCY),
        # Shared Flash capacity is independent of this layout pool's local
        # per-key request window. The pool does not create extra model quota.
        global_concurrency=DASHSCOPE_GLOBAL_CONCURRENCY,
        model=str(profile_model or TEMPLATE_CUT_MODEL or "").strip(),
        max_tokens=int(TEMPLATE_CUT_MAX_TOKENS),
        timeout_s=float(TEMPLATE_CUT_TIMEOUT_S),
        temperature=TEMPLATE_CUT_TEMPERATURE,
        stream=False,
    )


def _resolve_template_key_pool() -> KeyPool | None:
    """Resolve only the dedicated pay-as-you-go DashScope credentials.

    This function deliberately has no fallback to analyzer._llm_pool,
    LLM_API_KEYS, MiMo routes, or the paid grading credentials.
    """
    route = _template_cut_route()
    if not route.api_keys:
        return None
    return _cached_template_key_pool(
        list(route.api_keys),
        [route.base_url] * len(route.api_keys),
        list(route.api_key_weights) or None,
    )





def _local_template_capability_failed() -> bool:
    return time.monotonic() < _template_capability_unavailable_until


async def _shared_template_capability_failed(limiter) -> bool:
    reader = getattr(limiter, "is_provider_failure", None)
    if not callable(reader):
        return False
    return bool(await reader(_TEMPLATE_CAPABILITY))


async def _mark_template_capability_failure(limiter) -> None:
    global _template_capability_unavailable_until
    _template_capability_unavailable_until = max(
        _template_capability_unavailable_until,
        time.monotonic() + _CAPABILITY_FAILURE_COOLDOWN_S,
    )
    marker = getattr(limiter, "mark_provider_failure", None)
    if not callable(marker):
        return
    try:
        await marker(
            _TEMPLATE_CAPABILITY,
            int(_CAPABILITY_FAILURE_COOLDOWN_S),
        )
    except Exception:
        logger.exception("failed to share template capability failure state")


async def _clear_template_capability_failure(limiter) -> None:
    global _template_capability_unavailable_until
    _template_capability_unavailable_until = 0.0
    clearer = getattr(limiter, "clear_provider_failure", None)
    if not callable(clearer):
        return
    try:
        await clearer(_TEMPLATE_CAPABILITY)
    except Exception:
        logger.exception("failed to clear template capability failure state")


async def get_column_layout_readiness(limiter=None) -> dict[str, Any]:
    """Return column-layout readiness without sending a billable request."""
    template_route = _template_cut_route()
    pool = _resolve_template_key_pool()
    default_base_url = template_route.base_url
    model = str(template_route.model or "").strip()
    if pool is None:
        return {
            "provider": _PROVIDER,
            "configured": False,
            "ready": False,
            "status": "unavailable",
            "reason": "credentials_not_configured",
        }
    initial_state = pool.readiness()
    if not model:
        return {
            "provider": _PROVIDER,
            **initial_state,
            "configured": False,
            "ready": False,
            "status": "unavailable",
            "reason": "model_not_configured",
        }
    if not initial_state["ready"]:
        return {
            "provider": _PROVIDER,
            **initial_state,
            "ready": False,
            "status": "unavailable",
            "reason": "all_credentials_invalid",
        }
    routes = pool.enabled_credentials()
    if not default_base_url and not any(
        str(base_url or "").strip() for _, base_url in routes
    ):
        return {
            "provider": _PROVIDER,
            "configured": False,
            "ready": False,
            "status": "unavailable",
            "reason": "endpoint_not_configured",
        }

    try:
        limiter = limiter or get_model_limiter()
        await refresh_shared_credential_health(
            pool,
            limiter,
            provider=_PROVIDER,
            default_base_url=default_base_url,
            model=model,
        )
        shared_capability_failure = await _shared_template_capability_failed(limiter)
    except Exception:
        return {
            "provider": _PROVIDER,
            "configured": True,
            "ready": False,
            "status": "fault",
            "reason": "coordination_unavailable",
        }

    state = pool.readiness()
    if _local_template_capability_failed() or shared_capability_failure:
        status = "fault"
        reason = "runtime_circuit_open"
    elif not state["ready"]:
        status = "unavailable"
        reason = "all_credentials_invalid"
    elif state["immediately_available_keys"] < 1:
        status = "queued"
        reason = "credentials_busy_or_cooling_down"
    else:
        status = "available"
        reason = ""
    return {
        "provider": _PROVIDER,
        **state,
        "ready": bool(state["ready"] and status != "fault"),
        "status": status,
        "reason": reason,
    }


def _retry_after_seconds(response: httpx.Response) -> float | None:
    return parse_retry_after_seconds(response.headers.get("retry-after"))


async def _post_with_shared_lease(
    *,
    client: httpx.AsyncClient,
    limiter,
    api_key: str,
    base_url: str,
    pool: KeyPool,
    teacher_id: str,
    request_json: dict[str, Any],
    route=None,
    rate_budget_reservation=None,
) -> httpx.Response:
    request_started = False
    try:
        route = route or _template_cut_route()
        policy = configured_fair_share_policy(
            DASHSCOPE_GLOBAL_CONCURRENCY,
            provider=_PROVIDER,
        )
        lease = await limiter.acquire(
            provider=_PROVIDER,
            api_key=api_key,
            teacher_id=str(teacher_id or "system"),
            global_capacity=policy.global_capacity,
            # OCR/grading may already hold this same credential. Only the local
            # layout KeyPool uses route.key_concurrency; Redis sees the common cap.
            credential_capacity=DASHSCOPE_GLOBAL_CONCURRENCY,
            teacher_base_capacity=policy.teacher_base_capacity,
            teacher_burst_capacity=policy.teacher_burst_capacity,
            global_reserve_capacity=policy.global_reserve_capacity,
        )
        async with lease:
            request_started = True
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_json,
            )
    except BaseException as exc:
        # Refund only what never reached the provider; see dashscope_budget.
        if dashscope_budget.request_was_not_sent(request_started, exc):
            await dashscope_budget.cancel(limiter, rate_budget_reservation)
        raise
    await dashscope_budget.settle_response(limiter, rate_budget_reservation, response)
    return response


async def _acquire_template_rate_budget(*, limiter, route, request_json: dict[str, Any]):
    """Wait on the model's own DashScope RPM/TPM window (services/dashscope_budget)."""
    try:
        return await dashscope_budget.reserve(
            limiter,
            model=str(route.model),
            payload=request_json,
            completion_tokens=int(getattr(route, "max_tokens", 8192)),
        )
    except RateBudgetTimeout as exc:
        raise TemplateCutError(
            "PROVIDER_BUSY",
            "模板分栏的 RPM/TPM 额度暂时繁忙，请稍后重试",
            retryable=True,
        ) from exc


def _extract_json_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    fields = [message.get("content"), message.get("reasoning_content")]
    texts = [value.strip() for value in fields if isinstance(value, str) and value.strip()]
    for text in texts:
        if text.startswith("{"):
            return text
    for text in texts:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
    return texts[0] if texts else ""


def _integer_json_number(value: object) -> int | None:
    """Accept JSON numbers such as 500.0 only when they are exact integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _strict_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏返回结构无效")
    coordinate_space = raw.get("coordinate_space")
    if coordinate_space is not None and str(coordinate_space) != LAYOUT_COORDINATE_SPACE:
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏坐标合同无效")
    count = _integer_json_number(raw.get("column_count"))
    columns = raw.get("columns")
    if columns is None:
        compact_boundaries = raw.get("boundaries", raw.get("x_boundaries"))
        if isinstance(compact_boundaries, list):
            boundaries = [_integer_json_number(item) for item in compact_boundaries]
            if all(item is not None for item in boundaries):
                columns = [0, *boundaries, 1000]
    if (
        count is None
        or not 1 <= count <= 3
        or not isinstance(columns, list)
        or len(columns) != count
    ):
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏数量无效")

    try:
        normalized = _layout_columns_from_value(columns, allow_implicit_index=False)
    except TemplateCutError as exc:
        # Preserve the legacy provider error namespace and message while
        # retaining the stricter contiguous/minimum-width checks.
        message = str(exc)
        if "宽度过窄" in message:
            message = "分栏宽度过窄"
        elif "覆盖" in message or "连续" in message:
            message = "分栏边界无效"
        else:
            message = "分栏结构无效"
        raise TemplateCutError("PROVIDER_DATA_INVALID", message) from exc
    if len(normalized) != count:
        raise TemplateCutError("PROVIDER_DATA_INVALID", "分栏数量无效")
    try:
        _validate_crossing_evidence(normalized, raw)
    except TemplateCutError as exc:
        raise TemplateCutError("PROVIDER_DATA_INVALID", str(exc)) from exc
    return _payload_from_columns(
        normalized,
        source_key="model_column_layout",
        trust="model_coarse",
    )


def _infer_local_two_column_payload(
    image_path: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return a high-confidence landscape gutter split without a model call.

    The fast path deliberately recognizes only the common two-page/two-column
    case. Ambiguous pages, single columns and three-column layouts continue to
    the layout model instead of letting a heuristic become the authority.
    """
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as opened:
        source_width, source_height = opened.size
        if (
            source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH
            or source_width / float(source_height) < 1.10
        ):
            return None
        gray = np.asarray(opened.convert("L"))

    scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    height, width = gray.shape
    if width < 400 or height < 200:
        return None

    # Subtract slow camera-lighting variation before measuring the vertical
    # ink profile. This keeps a photographed gray page from looking fully dark.
    background = cv2.GaussianBlur(
        gray,
        (0, 0),
        sigmaX=max(4.0, width / 100.0),
        sigmaY=max(4.0, width / 100.0),
    )
    ink = (
        (gray.astype(np.int16) + 18 < background.astype(np.int16))
        & (gray < 210)
    )
    top = round(height * 0.12)
    bottom = round(height * 0.95)
    body = ink[top:bottom]
    if body.size == 0:
        return None

    profile = body.mean(axis=0)
    window = max(9, round(width * 0.014))
    if window % 2 == 0:
        window += 1
    smoothed = np.convolve(
        profile,
        np.ones(window, dtype=np.float64) / window,
        mode="same",
    )
    search_left = round(width * 0.42)
    search_right = round(width * 0.58)
    split = search_left + int(np.argmin(smoothed[search_left:search_right]))
    # A blank gutter is usually a plateau.  ``argmin`` alone chooses its left
    # edge, which needlessly narrows the first crop; use the midpoint of the
    # contiguous low-density valley while it remains inside the search band.
    valley_limit = max(float(smoothed[split]) + 0.003, 0.012)
    valley_left = split
    while valley_left > search_left and smoothed[valley_left - 1] <= valley_limit:
        valley_left -= 1
    valley_right = split
    while valley_right < search_right - 1 and smoothed[valley_right + 1] <= valley_limit:
        valley_right += 1
    split = round((valley_left + valley_right) / 2)
    side_pad = max(2, round(width * 0.015))
    left_body = body[:, : max(1, split - side_pad)]
    right_body = body[:, min(width - 1, split + side_pad):]
    if left_body.size == 0 or right_body.size == 0:
        return None

    gutter_density = float(smoothed[split])
    left_density = float(left_body.mean())
    right_density = float(right_body.mean())
    minimum_side_density = min(left_density, right_density)
    left_active_rows = float((left_body.mean(axis=1) > 0.003).mean())
    right_active_rows = float((right_body.mean(axis=1) > 0.003).mean())
    if (
        minimum_side_density < 0.012
        or min(left_active_rows, right_active_rows) < 0.28
        or gutter_density > 0.020
        or gutter_density > minimum_side_density * 0.38
    ):
        return None

    split_1000 = int(round(split * 1000 / width))
    # Do not turn a broad off-center blank gutter into a nominal 500 split.
    # The valley can easily span the page midpoint even when its actual
    # midpoint is several percent to the left or right.  Only snap when the
    # measured valley is already within a narrow rounding band around center;
    # otherwise retain the measured boundary so the crop cannot clip a column.
    if abs(split_1000 - 500) <= 8:
        center = round(width * 0.5)
        center_radius = max(4, round(width * 0.008))
        center_window = smoothed[
            max(search_left, center - center_radius):
            min(search_right, center + center_radius + 1)
        ]
        # Preserve the convenient 500 boundary only when the page confirms a
        # genuinely clear center window; a narrow off-center gutter keeps its
        # measured midpoint so it cannot clip one column's content.
        if (
            center_window.size
            and float(np.max(center_window)) <= max(valley_limit, 0.015)
        ):
            split_1000 = 500
    payload = _strict_payload({
        "column_count": 2,
        "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": split_1000},
            {"column_index": 2, "x1_1000": split_1000, "x2_1000": 1000},
        ],
    })
    for candidate in payload["candidates"]:
        candidate["source_key"] = "local_column_gutter"
        candidate["coordinate_trust"] = "local_high_confidence"
    return payload, {
        "strategy": "local_gutter_v1",
        "split_1000": split_1000,
        "gutter_density": round(gutter_density, 6),
        "left_density": round(left_density, 6),
        "right_density": round(right_density, 6),
    }


def _inspect_local_double_evidence(
    image_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Collect conservative, non-model evidence for a double-column draft.

    This is intentionally a review gate rather than a second layout authority.
    It detects common adversarial fixtures (a full-height printed seam, a line
    crossing the gutter, a single-column page, or a strongly off-centre split)
    and records why a candidate is or is not safe to expose to OCR.
    """
    import cv2
    import numpy as np
    from PIL import Image

    evidence: dict[str, Any] = {
        "evidence_version": _DOUBLE_EVIDENCE_VERSION,
        "strategy": "local_gutter_v1",
        # The gate thresholds travel with the evidence so a persisted
        # needs_review/ready decision can be audited against the exact
        # scoring rules that produced it, not the rules of a later release.
        "thresholds": dict(_DOUBLE_EVIDENCE_THRESHOLDS),
        "confidence": 0.0,
        "confidence_level": "low",
        "validation": "failed",
        "reasons": [],
    }
    try:
        with Image.open(image_path) as opened:
            source_width, source_height = opened.size
            gray = np.asarray(opened.convert("L"))
        if source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH:
            evidence["reasons"].append("source_too_narrow")
            return evidence
        scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
        if scale < 1.0:
            gray = cv2.resize(
                gray,
                (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = gray.shape
        if width < 400 or height < 200:
            evidence["reasons"].append("source_too_small")
            return evidence
        background = cv2.GaussianBlur(
            gray,
            (0, 0),
            sigmaX=max(4.0, width / 100.0),
            sigmaY=max(4.0, width / 100.0),
        )
        ink = (
            (gray.astype(np.int16) + 18 < background.astype(np.int16))
            & (gray < 210)
        )
        top = round(height * 0.12)
        bottom = round(height * 0.95)
        body = ink[top:bottom]
        if body.size == 0:
            evidence["reasons"].append("empty_body")
            return evidence
        profile = body.mean(axis=0)
        window = max(9, round(width * 0.014))
        if window % 2 == 0:
            window += 1
        smoothed = np.convolve(
            profile,
            np.ones(window, dtype=np.float64) / window,
            mode="same",
        )
        search_left = round(width * 0.35)
        search_right = round(width * 0.65)
        split = search_left + int(np.argmin(smoothed[search_left:search_right]))
        valley_limit = max(float(smoothed[split]) + 0.003, 0.012)
        valley_left = split
        while valley_left > search_left and smoothed[valley_left - 1] <= valley_limit:
            valley_left -= 1
        valley_right = split
        while valley_right < search_right - 1 and smoothed[valley_right + 1] <= valley_limit:
            valley_right += 1
        split = round((valley_left + valley_right) / 2)
        radius = max(3, round(width * 0.012))
        seam_start = max(0, split - radius)
        seam_end = min(width, split + radius + 1)
        seam = body[:, seam_start:seam_end]
        left = body[:, :max(1, split - radius)]
        right = body[:, min(width - 1, split + radius):]
        seam_density = float(seam.mean()) if seam.size else 1.0
        left_density = float(left.mean()) if left.size else 0.0
        right_density = float(right.mean()) if right.size else 0.0
        side_min = min(left_density, right_density)
        side_max = max(left_density, right_density)
        side_ratio = side_max / max(side_min, 1e-6)
        seam_active_rows = float((seam.mean(axis=1) > 0.025).mean()) if seam.size else 1.0
        left_active_rows = float((left.mean(axis=1) > 0.003).mean()) if left.size else 0.0
        right_active_rows = float((right.mean(axis=1) > 0.003).mean()) if right.size else 0.0
        central_left = round(width * 0.40)
        central_right = round(width * 0.60)
        central_profile = profile[central_left:central_right]
        vertical_peak = float(central_profile.max()) if central_profile.size else 0.0
        vertical_peak_x = (
            central_left + int(np.argmax(central_profile))
            if central_profile.size
            else split
        )

        # A row with a long contiguous run through the gutter is a printed
        # cross-line, not evidence of a column seam.  Restrict this one check
        # to the question body: page rectification can turn the bottom border
        # into a thick full-width component, while the other evidence below
        # still accounts for content in the bottom margin.
        t = _DOUBLE_EVIDENCE_THRESHOLDS
        cross_line_scan_bottom = max(
            0,
            min(
                body.shape[0],
                round(height * float(t["cross_line_scan_bottom_fraction"])) - top,
            ),
        )
        cross_line_rows = 0
        for row in body[:cross_line_scan_bottom]:
            run = 0
            longest = 0
            for value in row.tolist():
                if value:
                    run += 1
                    longest = max(longest, run)
                else:
                    run = 0
            if longest >= max(50, round(width * 0.55)):
                cross_line_rows += 1
        cross_line_fraction = cross_line_rows / max(1, cross_line_scan_bottom)

        # Connected components that genuinely straddle the proposed gutter are
        # usually a question number, answer box, or other content crossing the
        # seam.  Very wide/full-height components are handled by the line
        # checks above and are not double-counted here.
        crossing_components = 0
        try:
            component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
                ink.astype(np.uint8), connectivity=8
            )
            for component_index in range(1, component_count):
                x, _y, component_width, component_height, _area = stats[component_index]
                component_right = int(x + component_width)
                if (
                    int(x) < split
                    and component_right > split
                    and component_width < round(width * 0.45)
                    and component_height < round(height * 0.80)
                ):
                    crossing_components += 1
        except Exception:
            crossing_components = 0

        split_1000 = int(round(split * 1000 / width))
        center_offset = abs(split_1000 - 500)
        broad_left = round(width * 0.20)
        broad_right = round(width * 0.80)
        broad_profile = smoothed[broad_left:broad_right]
        broad_min_x = (
            broad_left + int(np.argmin(broad_profile))
            if broad_profile.size
            else split
        )
        broad_min_1000 = int(round(broad_min_x * 1000 / width))
        reasons: list[str] = []
        if vertical_peak >= t["vertical_peak_density_max"]:
            reasons.append("printed_vertical_line_or_content_crossing_gutter")
        if seam_active_rows >= t["seam_active_rows_max"] and seam_density >= max(
            t["seam_density_floor"], side_min * t["seam_to_side_ratio_max"]
        ):
            if "printed_vertical_line_or_content_crossing_gutter" not in reasons:
                reasons.append("printed_vertical_line_or_content_crossing_gutter")
        if cross_line_fraction > t["cross_line_fraction_max"]:
            reasons.append("printed_line_crosses_gutter")
        if crossing_components:
            reasons.append("question_or_answer_crosses_boundary")
        if (
            side_min < t["side_density_min"]
            or min(left_active_rows, right_active_rows) < t["side_active_rows_min"]
        ):
            reasons.append("insufficient_two_sided_content")
        if side_ratio > t["left_right_ratio_max"]:
            reasons.append("left_right_content_ratio_unbalanced")
        if center_offset > t["center_offset_1000_max"]:
            reasons.append("off_center_boundary")
        # A clear valley well outside the central band is a valid observation
        # but not a safe automatic double split: require teacher confirmation
        # so a non-equal page cannot be silently clipped.
        if (
            broad_min_1000 < t["broad_valley_1000_min"]
            or broad_min_1000 > t["broad_valley_1000_max"]
        ):
            if float(smoothed[broad_min_x]) <= t["broad_valley_density_max"]:
                reasons.append("off_center_boundary")
        safe = not reasons
        # Confidence is deliberately capped below certainty.  The teacher
        # confirmation page remains the authority even for a clear gutter.
        confidence = 0.92
        confidence -= min(0.35, seam_density * 2.0)
        confidence -= min(0.20, max(0.0, side_ratio - 1.0) * 0.08)
        confidence -= min(0.20, center_offset / 500.0)
        if not safe:
            confidence = min(confidence, 0.39)
        evidence.update({
            "split_1000": split_1000,
            "gutter_density": round(seam_density, 6),
            "left_density": round(left_density, 6),
            "right_density": round(right_density, 6),
            "left_right_ratio": round(side_ratio, 4),
            "seam_active_rows": round(seam_active_rows, 4),
            "left_active_rows": round(left_active_rows, 4),
            "right_active_rows": round(right_active_rows, 4),
            "cross_line_fraction": round(cross_line_fraction, 6),
            "cross_line_scan_bottom_y": top + cross_line_scan_bottom,
            "crossing_components": crossing_components,
            "center_offset_1000": center_offset,
            "broad_valley_1000": broad_min_1000,
            "vertical_peak_density": round(vertical_peak, 6),
            "vertical_peak_x1000": int(round(vertical_peak_x * 1000 / width)),
            "reasons": reasons,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "confidence_level": "high" if safe else "low",
            "validation": "passed" if safe else "failed",
        })
        return evidence
    except Exception as exc:
        # Do not leak paths or decoder details into a teacher-facing response.
        evidence["reasons"] = ["evidence_unavailable"]
        evidence["error_type"] = type(exc).__name__
        return evidence


def _inspect_local_triple_evidence(
    image_path: str | os.PathLike[str],
    columns: list[dict[str, int]],
) -> dict[str, Any]:
    """Collect per-column content evidence for a model-proposed triple layout.

    The model only proposes the two seams.  Before a proposal may freeze crops,
    every column must contain printed content and every seam must be a
    low-ink valley with no printed line running through it.  The thresholds
    are copied into the record so the decision stays auditable.
    """
    import cv2
    import numpy as np
    from PIL import Image

    t = _TRIPLE_EVIDENCE_THRESHOLDS
    evidence: dict[str, Any] = {
        "evidence_version": _TRIPLE_EVIDENCE_VERSION,
        "strategy": "local_triple_content_v1",
        "thresholds": dict(t),
        "confidence": 0.0,
        "confidence_level": "low",
        "validation": "failed",
        "reasons": [],
        "columns": [],
        "seams": [],
    }
    try:
        with Image.open(image_path) as opened:
            source_width, source_height = opened.size
            gray = np.asarray(opened.convert("L"))
        if source_width < _LOCAL_LAYOUT_MIN_SOURCE_WIDTH:
            evidence["reasons"].append("source_too_narrow")
            return evidence
        scale = min(1.0, _LOCAL_LAYOUT_MAX_WIDTH / float(source_width))
        if scale < 1.0:
            gray = cv2.resize(
                gray,
                (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = gray.shape
        if width < 400 or height < 200:
            evidence["reasons"].append("source_too_small")
            return evidence
        background = cv2.GaussianBlur(
            gray,
            (0, 0),
            sigmaX=max(4.0, width / 100.0),
            sigmaY=max(4.0, width / 100.0),
        )
        ink = (
            (gray.astype(np.int16) + 18 < background.astype(np.int16))
            & (gray < 210)
        )
        body = ink[round(height * 0.12):round(height * 0.95)]
        if body.size == 0:
            evidence["reasons"].append("empty_body")
            return evidence

        reasons: list[str] = []
        pad = max(2, round(width * 0.012))
        side_densities: list[float] = []
        for column in columns:
            left = round(int(column["x1_1000"]) * width / 1000)
            right = round(int(column["x2_1000"]) * width / 1000)
            inner = body[:, min(right - 1, left + pad):max(left + 1, right - pad)]
            density = float(inner.mean()) if inner.size else 0.0
            active_rows = float((inner.mean(axis=1) > 0.003).mean()) if inner.size else 0.0
            side_densities.append(density)
            evidence["columns"].append({
                "column_index": int(column["column_index"]),
                "density": round(density, 6),
                "active_rows": round(active_rows, 4),
            })
            if density < t["column_density_min"] or active_rows < t["column_active_rows_min"]:
                reasons.append(f"insufficient_column_content:col_{int(column['column_index'])}")

        for position, column in enumerate(columns[:-1]):
            boundary = round(int(column["x2_1000"]) * width / 1000)
            seam = body[:, max(0, boundary - pad):min(width, boundary + pad + 1)]
            seam_density = float(seam.mean()) if seam.size else 1.0
            neighbours = min(side_densities[position], side_densities[position + 1])
            # A row whose ink runs continuously across the seam is a printed
            # line, not a gutter.
            cross_rows = 0
            for row in seam:
                if row.size and bool(row.all()):
                    cross_rows += 1
            cross_fraction = cross_rows / max(1, seam.shape[0])
            evidence["seams"].append({
                "boundary_1000": int(column["x2_1000"]),
                "density": round(seam_density, 6),
                "cross_line_fraction": round(cross_fraction, 6),
            })
            if seam_density > t["seam_density_max"] or (
                neighbours > 0 and seam_density > neighbours * t["seam_to_side_ratio_max"]
            ):
                reasons.append(f"seam_not_blank:{int(column['x2_1000'])}")
            if cross_fraction > t["cross_line_fraction_max"]:
                reasons.append("printed_line_crosses_gutter")

        safe = not reasons
        confidence = 0.9 if safe else 0.3
        if side_densities:
            spread = max(side_densities) / max(min(side_densities), 1e-6)
            confidence -= min(0.2, max(0.0, spread - 1.0) * 0.05)
        evidence.update({
            "reasons": reasons,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "confidence_level": "high" if safe else "low",
            "validation": "passed" if safe else "failed",
        })
        return evidence
    except Exception as exc:
        evidence["reasons"] = ["evidence_unavailable"]
        evidence["error_type"] = type(exc).__name__
        return evidence


def _double_layout_draft(
    image_path: str | os.PathLike[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a code-proposed double layout and its review evidence."""
    evidence = _inspect_local_double_evidence(image_path)
    try:
        inferred = _infer_local_two_column_payload(image_path)
    except Exception:
        inferred = None
    if inferred is None:
        return None, evidence
    payload, diagnostics = inferred
    merged = {**diagnostics, **evidence}
    if evidence.get("validation") != "passed":
        return payload, merged
    for candidate in payload.get("candidates", []):
        candidate["coordinate_trust"] = "local_high_confidence"
    return payload, merged


def _coerce_revision(value: object, *, default: int = 1) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    parsed = _integer_json_number(value)
    if parsed is None and isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            parsed = None
    if parsed is None or parsed < 1:
        raise TemplateCutError("LAYOUT_REVISION_INVALID", "layout_revision 必须是正整数")
    return int(parsed)


def _layout_signature(layout: object) -> tuple[object, ...] | None:
    if not isinstance(layout, dict):
        return None
    column_layout = layout.get("column_layout")
    if not isinstance(column_layout, dict):
        column_layout = layout
    raw_columns = column_layout.get("columns")
    if not isinstance(raw_columns, list):
        return None
    signature: list[object] = [
        str(
            layout.get("layout_mode")
            or layout.get("column_mode")
            or column_layout.get("layout_mode")
            or column_layout.get("column_mode")
            or ""
        ).strip().lower(),
        str(layout.get("source", {}).get("sha256") or "")
        if isinstance(layout.get("source"), dict)
        else str(layout.get("image_sha256") or ""),
    ]
    for raw in raw_columns:
        if not isinstance(raw, dict):
            return None
        left = _as_exact_int(raw.get("x1_1000"))
        right = _as_exact_int(raw.get("x2_1000"))
        if left is None or right is None:
            return None
        signature.append((left, right))
    return tuple(signature)


def _next_layout_revision(
    *,
    previous_layout: object = None,
    requested_revision: object = None,
    mode: str,
    columns: list[dict[str, int]],
    source_sha256: str,
) -> int:
    """Return a monotonic draft revision without persisting state."""
    requested = _coerce_revision(requested_revision, default=1)
    if not isinstance(previous_layout, dict):
        return requested
    previous_revision = _coerce_revision(previous_layout.get("layout_revision"), default=1)
    current_signature = (mode, source_sha256, *(
        (item["x1_1000"], item["x2_1000"]) for item in columns
    ))
    previous_signature = _layout_signature(previous_layout)
    if previous_signature is None:
        return max(requested, previous_revision + 1)
    if previous_signature == current_signature:
        return max(requested, previous_revision)
    return max(requested, previous_revision + 1)


def _enrich_layout_columns(
    source: dict[str, Any],
    columns: list[dict[str, int]],
    *,
    layout_revision: int,
    image_sha256: str,
) -> list[dict[str, Any]]:
    width = int(source["width"])
    height = int(source["height"])
    enriched: list[dict[str, Any]] = []
    for position, raw in enumerate(columns, 1):
        left = int(raw["x1_1000"])
        right = int(raw["x2_1000"])
        crop_left = round(left * width / 1000)
        crop_right = round(right * width / 1000)
        identity_material = f"{image_sha256}:{layout_revision}:{position}:{left}:{right}"
        column_id = "col_{}-{}".format(
            position,
            hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:16],
        )
        enriched.append({
            "column_index": position,
            "column_order": position,
            "order": position,
            "ordinal": position,
            "column_id": column_id,
            "x1_1000": left,
            "x2_1000": right,
            "crop_left": crop_left,
            "crop_right": crop_right,
            "crop_left_px": crop_left,
            "crop_right_px": crop_right,
            "pixel_crop_left": crop_left,
            "pixel_crop_right": crop_right,
            "crop_box_px": [crop_left, 0, crop_right, height],
            "crop_coordinate_space": "source_pixels",
            "crop_endpoint_version": PIXEL_ENDPOINT_VERSION,
            "actual_pixel_endpoints": {
                "left": crop_left,
                "right": crop_right,
            },
            "source_width": width,
            "source_height": height,
            "source_dimensions": {"width": width, "height": height},
            "image_sha256": image_sha256,
            "image_hash": image_sha256,
            "layout_revision": layout_revision,
            "coordinate_space": LAYOUT_COORDINATE_SPACE,
        })
    return enriched


def _build_layout_result(
    source: dict[str, Any],
    payload: dict[str, Any],
    *,
    provider: str,
    request_id: str = "",
    diagnostics: dict[str, Any] | None = None,
    column_mode: str | None = None,
    layout_revision: object = None,
    previous_layout: object = None,
    layout_status: str = "ready",
) -> dict[str, Any]:
    raw_layout = payload.get("column_layout") if isinstance(payload, dict) else None
    if not isinstance(raw_layout, dict):
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "布局缺少 column_layout")
    raw_columns = raw_layout.get("columns")
    if (
        layout_status == "needs_review"
        and isinstance(raw_columns, list)
        and not raw_columns
    ):
        columns: list[dict[str, int]] = []
    else:
        columns = _layout_columns_from_value(raw_columns)
    mode = normalize_column_mode(
        column_mode,
        default=("single" if len(columns) == 1 else "triple" if len(columns) == 3 else DEFAULT_COLUMN_MODE),
    )
    if columns and mode == "single" and len(columns) != 1:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "单栏边界数量无效")
    if columns and mode == "double" and len(columns) != 2:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "双栏边界数量无效")
    if columns and mode == "triple" and len(columns) != 3:
        raise TemplateCutError("LAYOUT_BOUNDARY_INVALID", "三栏边界数量无效")
    revision = _next_layout_revision(
        previous_layout=previous_layout,
        requested_revision=layout_revision,
        mode=mode,
        columns=columns,
        source_sha256=str(source["sha256"]),
    )
    enriched_columns = _enrich_layout_columns(
        source,
        columns,
        layout_revision=revision,
        image_sha256=str(source["sha256"]),
    )
    candidates: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(payload.get("candidates") or []):
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        if candidate_index < len(enriched_columns):
            item.update(enriched_columns[candidate_index])
            item["template_bbox_1000"] = [
                enriched_columns[candidate_index]["x1_1000"],
                0,
                enriched_columns[candidate_index]["x2_1000"],
                1000,
            ]
        item["candidate_index"] = candidate_index
        item["layout_revision"] = revision
        item["image_sha256"] = str(source["sha256"])
        candidates.append(item)
    # A review draft may intentionally omit candidates so downstream OCR cannot
    # mistake an untrusted boundary for an approved crop.
    if layout_status == "needs_review" and not candidates:
        candidates = []
    normalized_layout = {
        "column_count": len(enriched_columns),
        "columns": enriched_columns,
        "boundaries_1000": [item["x2_1000"] for item in enriched_columns[:-1]],
        "layout_mode": mode,
        "column_mode": mode,
        "layout_revision": revision,
        "coordinate_space": LAYOUT_COORDINATE_SPACE,
        "crop_endpoint_version": PIXEL_ENDPOINT_VERSION,
        "source_width": int(source["width"]),
        "source_height": int(source["height"]),
        "image_sha256": str(source["sha256"]),
        "contract_version": LAYOUT_CONTRACT_VERSION,
    }
    result = {
        "layout_contract_version": LAYOUT_CONTRACT_VERSION,
        "coordinate_space": LAYOUT_COORDINATE_SPACE,
        "provider": provider,
        "operation": _OPERATION,
        "cut_type": "question",
        "image_type": "template",
        "subject": "Math",
        "output_oricoord": True,
        "request_id": request_id,
        "layout_mode": mode,
        "column_mode": mode,
        "layout_revision": revision,
        "layout_status": layout_status,
        "status": layout_status,
        "needs_review": layout_status == "needs_review",
        "usable_for_ocr": layout_status != "needs_review",
        "draft": True,
        "source": {
            "sha256": source["sha256"],
            "image_sha256": source["sha256"],
            "image_hash": source["sha256"],
            "width": source["width"],
            "height": source["height"],
            "size_bytes": source["size_bytes"],
            "image_format": source["image_format"],
        },
        "provider_dimensions": {
            "org_width": source["width"],
            "org_height": source["height"],
            "rectified_width": source["width"],
            "rectified_height": source["height"],
        },
        "pages": [{
            "page_index": 0,
            "provider_page_no": 1,
        "layout_revision": revision,
        "source_width": int(source["width"]),
        "source_height": int(source["height"]),
        "source_dimensions": {
            "width": int(source["width"]),
            "height": int(source["height"]),
        },
        "image_sha256": str(source["sha256"]),
        "image_hash": str(source["sha256"]),
            "provider_dimensions": {
                "org_width": source["width"],
                "org_height": source["height"],
                "rectified_width": source["width"],
                "rectified_height": source["height"],
            },
            "candidates": candidates,
        }],
        "summary": {
            "total": len(candidates),
            "original": len(candidates),
            "rectified_untrusted": 0,
            "invalid": 0,
            "missing": 0,
        },
        "column_layout": normalized_layout,
    }
    if diagnostics is not None:
        result["layout_diagnostics"] = {
            "contract_version": LAYOUT_CONTRACT_VERSION,
            "coordinate_space": LAYOUT_COORDINATE_SPACE,
            "layout_revision": revision,
            **diagnostics,
        }
    return result


def _review_layout_result(
    source: dict[str, Any],
    *,
    mode: str,
    diagnostics: dict[str, Any] | None = None,
    layout_revision: object = None,
    previous_layout: object = None,
    proposed_columns: object = None,
    provider: str = "local_projection",
) -> dict[str, Any]:
    """Build a fail-closed draft for the confirmation page."""
    # A proposed boundary may be shown to a teacher, but it is never marked
    # usable until the teacher confirms it.  Invalid/unknown proposals are
    # represented by an empty column list rather than a nominal equal split.
    columns: list[dict[str, int]] = []
    if proposed_columns is not None:
        try:
            columns = _layout_columns_from_value(proposed_columns)
            if (mode == "double" and len(columns) != 2) or (
                mode == "triple" and len(columns) != 3
            ) or (mode == "single" and len(columns) != 1):
                columns = []
        except TemplateCutError:
            columns = []
    if columns:
        payload = _payload_from_columns(
            columns,
            source_key="layout_review_candidate",
            trust="local_review",
        )
        # Keep review candidates visible but explicitly unusable.
        for candidate in payload["candidates"]:
            candidate["usable_for_original_overlay"] = False
    else:
        payload = {
            "column_layout": {
                "column_count": {"single": 1, "double": 2, "triple": 3}[mode],
                "columns": [],
            },
            "candidates": [],
        }
    if not columns:
        result = _build_layout_result(
            source,
            payload,
            provider=provider,
            diagnostics=diagnostics,
            column_mode=mode,
            layout_revision=layout_revision,
            previous_layout=previous_layout,
            layout_status="needs_review",
        )
        return result
    return _build_layout_result(
        source,
        payload,
        provider=provider,
        diagnostics=diagnostics,
        column_mode=mode,
        layout_revision=layout_revision,
        previous_layout=previous_layout,
        layout_status="needs_review",
    )


def revise_layout_draft(
    layout: dict[str, Any],
    *,
    column_mode: object = None,
    layout_mode: object = None,
    column_count: object = None,
    mode: object = None,
    requested_mode: object = None,
    columns: object = None,
    boundaries: object = None,
    evidence: object = None,
) -> dict[str, Any]:
    """Return an edited layout draft without persisting a final contract.

    The source image metadata is copied from the prior draft.  A changed mode
    or boundary increments ``layout_revision``; unchanged edits retain it so
    cached downstream inputs remain identifiable and reusable.
    """
    if not isinstance(layout, dict) or not isinstance(layout.get("source"), dict):
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿缺少源图信息")
    supplied = [
        item
        for item in (column_mode, layout_mode, column_count, mode, requested_mode)
        if item is not None
    ]
    if len(supplied) > 1:
        normalized_values = [normalize_column_mode(item) for item in supplied]
        if len(set(normalized_values)) != 1:
            raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式参数冲突")
        mode = normalized_values[0]
    elif supplied:
        mode = normalize_column_mode(supplied[0])
    else:
        mode = normalize_column_mode(
            layout.get("layout_mode") or layout.get("column_mode")
        )
    boundary_value = boundaries if boundaries is not None else columns
    if boundary_value is None:
        old_layout = layout.get("column_layout")
        boundary_value = old_layout.get("columns") if isinstance(old_layout, dict) else None
    validated = validate_layout_boundaries(
        boundary_value,
        column_mode=mode,
        evidence=evidence,
    )
    source = layout["source"]
    source_info = {
        "sha256": str(source.get("sha256") or source.get("image_sha256") or ""),
        "width": int(source.get("width") or source.get("source_width") or 0),
        "height": int(source.get("height") or source.get("source_height") or 0),
        "size_bytes": int(source.get("size_bytes") or 0),
        "image_format": str(source.get("image_format") or ""),
    }
    if not source_info["sha256"] or source_info["width"] <= 0 or source_info["height"] <= 0:
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿源图信息无效")
    payload = _payload_from_columns(
        validated,
        source_key="teacher_layout_draft",
        trust="teacher_confirmed_draft",
    )
    # A source-only stub (a legacy page that never had a layout draft) is not
    # a prior revision: the teacher's first boundary set starts at revision 1
    # instead of pretending an empty draft was edited.
    has_prior_columns = isinstance(layout.get("column_layout"), dict)
    return _build_layout_result(
        source_info,
        payload,
        provider=str(layout.get("provider") or "layout_draft"),
        request_id=str(layout.get("request_id") or ""),
        diagnostics={
            "strategy": "teacher_draft_edit",
            "validation": "passed",
            "confidence": 1.0,
            "confidence_level": "teacher_confirmed_draft",
        },
        column_mode=mode,
        layout_revision=layout.get("layout_revision"),
        previous_layout=layout if has_prior_columns else None,
        layout_status="ready",
    )


def layout_needs_review(layout: object) -> bool:
    """Whether a layout draft is still a teacher-review candidate.

    Legacy plans without a status are treated as usable; only an explicit
    ``needs_review`` status or ``usable_for_ocr=False`` blocks downstream use.
    """
    if not isinstance(layout, dict):
        return False
    if layout.get("usable_for_ocr") is False:
        return True
    return str(layout.get("layout_status") or layout.get("status") or "").strip().lower() == "needs_review"


def whole_page_fallback_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """Return the one whole-page OCR input used while a column draft needs review.

    An unusable or low-confidence column proposal stays the teacher-facing
    draft.  The model routes for that page fall back to a single whole-page
    request instead of unverified column crops; this layout only organises
    that request and is labelled so it cannot be mistaken for an approved
    column result.
    """
    if not isinstance(layout, dict) or not isinstance(layout.get("source"), dict):
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿缺少源图信息")
    source = layout["source"]
    source_info = {
        "sha256": str(source.get("sha256") or source.get("image_sha256") or ""),
        "width": int(source.get("width") or source.get("source_width") or 0),
        "height": int(source.get("height") or source.get("source_height") or 0),
        "size_bytes": int(source.get("size_bytes") or 0),
        "image_format": str(source.get("image_format") or ""),
    }
    if not source_info["sha256"] or source_info["width"] <= 0 or source_info["height"] <= 0:
        raise TemplateCutError("LAYOUT_DRAFT_INVALID", "布局草稿源图信息无效")
    payload = _payload_from_columns(
        [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        source_key="whole_page_fallback",
        trust="whole_page_fallback",
    )
    review_diagnostics = layout.get("layout_diagnostics")
    result = _build_layout_result(
        source_info,
        payload,
        provider="local_whole_page_fallback",
        diagnostics={
            "strategy": "whole_page_fallback",
            "validation": "passed",
            "confidence": 1.0,
            "confidence_level": "fallback",
            "review_mode": str(layout.get("layout_mode") or layout.get("column_mode") or ""),
            "review_reasons": list(
                (review_diagnostics or {}).get("reasons") or []
            ) if isinstance(review_diagnostics, dict) else [],
        },
        column_mode="single",
        layout_revision=layout.get("layout_revision"),
        previous_layout=None,
        layout_status="ready",
    )
    result["fallback_for_review"] = True
    return result


# Descriptive aliases used by confirmation-page integrations.
update_layout_draft = revise_layout_draft
edit_layout_draft = revise_layout_draft


async def recognize_column_layout(
    image_path: str | os.PathLike[str],
    *,
    client: httpx.AsyncClient | None = None,
    teacher_id: str = "system",
    column_mode: object = None,
    layout_mode: object = None,
    column_count: object = None,
    mode: object = None,
    requested_mode: object = None,
    layout_revision: object = None,
    previous_layout: object = None,
) -> dict[str, Any]:
    """Recognize a one-, two-, or three-column layout from one teacher template.

    The documented path is selected with ``column_mode`` (or its compatible
    ``layout_mode``/``column_count`` spellings).  Single-column pages return a
    whole-page draft directly; double-column pages use only the local code
    gate; triple-column pages use the layout model only to propose boundaries
    are validated by this module.  Calls that predate the mode argument and
    explicitly inject an HTTP client retain the legacy provider behavior for
    compatibility with existing integrations.
    """
    from services.ocr_service import (
        _get_local_ocr_payload_budget,
        _run_local_image_thread,
        _run_local_ocr_image_component,
    )

    supplied_modes = [
        value for value in (
            column_mode,
            layout_mode,
            column_count,
            mode,
            requested_mode,
        )
        if value is not None
    ]
    if len(supplied_modes) > 1:
        normalized_modes = [normalize_column_mode(value) for value in supplied_modes]
        if len(set(normalized_modes)) != 1:
            raise TemplateCutError("LAYOUT_MODE_INVALID", "分栏模式参数冲突")
        mode = normalized_modes[0]
        explicit_mode = True
    elif supplied_modes:
        mode = normalize_column_mode(supplied_modes[0])
        explicit_mode = True
    else:
        mode = DEFAULT_COLUMN_MODE
        explicit_mode = False

    source = await _run_local_ocr_image_component(
        _validate_template_image,
        image_path,
    )
    # Calls that predate the mode argument remain on the old low-level path so
    # existing integrations can still inject a provider client (and retain
    # their retry/error semantics).  New callers should pass the normalized
    # mode explicitly; those calls always obey the single/double/triple draft
    # contract below.  ``mode`` is nevertheless initialized to the documented
    # double default for metadata and validation helpers.
    legacy_provider_path = not explicit_mode

    if not legacy_provider_path and mode == "single":
        payload = _payload_from_columns(
            [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
            source_key="single_page_direct",
            trust="single_page_direct",
        )
        return _build_layout_result(
            source,
            payload,
            provider="local_direct",
            diagnostics={
                "strategy": "single_direct",
                "validation": "passed",
                "confidence": 1.0,
                "confidence_level": "high",
                "evidence": {"whole_page_boundary": True, "model_called": False},
            },
            column_mode="single",
            layout_revision=layout_revision,
            previous_layout=previous_layout,
        )

    double_local_columns = None
    double_local_evidence = None
    if not legacy_provider_path and mode == "double":
        local_layout = await _run_local_ocr_image_component(
            _double_layout_draft,
            source["path"],
        )
        payload, diagnostics = local_layout
        if payload is not None and diagnostics.get("validation") == "passed":
            return _build_layout_result(
                source,
                payload,
                provider="local_projection",
                diagnostics=diagnostics,
                column_mode="double",
                layout_revision=layout_revision,
                previous_layout=previous_layout,
            )
        # 本地像素证据不达标不再停在 needs_review 死胡同：双栏边界改由
        # qwen3.8-flash（官方关闭思考组合）给出，本地证据降级为诊断记录，
        # 教师确认页仍是最终闸门。模型不可用时按传输错误上抛，不伪造草稿。
        double_local_columns = (
            payload.get("column_layout", {}).get("columns") if payload else None
        )
        double_local_evidence = diagnostics

    elif client is None and mode != "triple":
        try:
            local_layout = await _run_local_ocr_image_component(
                _infer_local_two_column_payload,
                source["path"],
            )
        except Exception as exc:
            # The heuristic is an optimization, never an authority. Decoder or
            # image-library failures must preserve the existing model path.
            logger.warning("Local template gutter detection failed: %s", exc)
            local_layout = None
        if local_layout is not None:
            payload, diagnostics = local_layout
            logger.info(
                "Template column layout completed locally strategy=%s split_1000=%s",
                diagnostics["strategy"],
                diagnostics["split_1000"],
            )
            return _build_layout_result(
                source,
                payload,
                provider="local_projection",
                diagnostics=diagnostics,
            )
    pool = _resolve_template_key_pool()
    template_route = _template_cut_route()
    model = str(template_route.model or "").strip()
    if pool is None or not model:
        raise TemplateCutError("CREDENTIAL_UNAVAILABLE", "模板分栏配置不完整")
    mime = "image/png" if source["image_format"] == "png" else "image/jpeg"
    owned_client = client is None
    limiter = get_model_limiter()
    await refresh_shared_credential_health(
        pool,
        limiter,
        provider=_PROVIDER,
        default_base_url=template_route.base_url,
        model=model,
    )
    if (
        _local_template_capability_failed()
        or await _shared_template_capability_failed(limiter)
    ):
        raise TemplateCutError(
            "PROVIDER_BUSY",
            "template column recognition is temporarily unavailable",
            retryable=True,
        )
    if not pool.has_enabled_keys():
        raise TemplateCutError(
            "CREDENTIAL_UNAVAILABLE",
            "模板分栏凭证均不可用",
        )
    if pool.healthy_key_count() < 1:
        raise TemplateCutError(
            "PROVIDER_BUSY",
            "模板分栏凭证暂时限流，请稍后重试",
            retryable=True,
        )
    if client is None:
        timeout_s = template_route.timeout_s
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            trust_env=False,
        )

    # OCR and template vision share one process-level byte budget so their
    # Base64/JSON allocations cannot independently exhaust the worker.
    payload_reservation = _get_local_ocr_payload_budget().reserve(
        _estimated_template_payload_bytes(source["size_bytes"])
    )
    try:
        await asyncio.wait_for(
            payload_reservation.__aenter__(),
            timeout=_KEY_ACQUIRE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        if owned_client:
            await client.aclose()
        raise TemplateCutError(
            "IMAGE_MEMORY_BUSY",
            "模板分栏图片内存容量繁忙，请稍后重试",
            retryable=True,
        ) from exc
    except ValueError as exc:
        if owned_client:
            await client.aclose()
        raise TemplateCutError(
            "IMAGE_MEMORY_LIMIT",
            "模板分栏图片超过当前工作进程内存预算",
        ) from exc
    except asyncio.CancelledError:
        if owned_client:
            await client.aclose()
        raise
    except Exception:
        if owned_client:
            await client.aclose()
        raise
    last_error: TemplateCutError | None = None
    body: object = None
    payload: dict[str, Any] | None = None
    data_url: str | None = None
    request_json: dict[str, Any] | None = None
    response: httpx.Response | None = None
    transient_attempt = 0
    capability_failure_observed = False
    try:
        data_url = await _run_local_image_thread(
            _encode_template_data_url,
            source["path"],
            mime,
        )
        prompt_text = (
            _TRIPLE_PROMPT
            if mode == "triple"
            else _DOUBLE_PROMPT
            if explicit_mode and mode == "double"
            else _PROMPT
        )
        request_json = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            }],
            "temperature": TEMPLATE_CUT_TEMPERATURE,
            "max_tokens": TEMPLATE_CUT_MAX_TOKENS,
            "stream": False,
            # Qwen 官方关闭组合（两键同发，qwen3.8-flash 线上验证）：分栏不走
            # 思考，reasoning_tokens=0。思考开启时官方仅支持流式调用，这里固定
            # 关闭 + 非流式是官方允许的组合。
            "enable_thinking": TEMPLATE_CUT_ENABLE_THINKING,
            "reasoning_effort": TEMPLATE_CUT_REASONING_EFFORT,
        }
        assert request_json is not None
        while transient_attempt < _MAX_TRANSIENT_ATTEMPTS:
            try:
                api_key, release_key, pool_base_url = await asyncio.wait_for(
                    acquire_key(pool),
                    timeout=_KEY_ACQUIRE_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                if last_error is not None:
                    raise last_error from exc
                raise TemplateCutError(
                    "CREDENTIAL_UNAVAILABLE",
                    "模板分栏凭证均不可用",
                ) from exc
            except TimeoutError as exc:
                raise TemplateCutError(
                    "PROVIDER_BUSY",
                    "模板分栏容量繁忙，请稍后重试",
                    retryable=True,
                ) from exc

            released = False

            def release_route() -> None:
                nonlocal released
                if not released:
                    pool.release(release_key)
                    released = True

            base_url = str(pool_base_url or template_route.base_url or "").strip()
            if not base_url:
                release_route()
                raise TemplateCutError(
                    "CREDENTIAL_UNAVAILABLE",
                    "模板分栏配置不完整",
                )

            try:
                if await limiter.is_credential_invalid(
                    provider=_PROVIDER,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                ):
                    release_route()
                    pool.mark_invalid(api_key, 401)
                    last_error = TemplateCutError(
                        "CREDENTIAL_UNAVAILABLE",
                        "模板分栏凭证均不可用",
                    )
                    if pool.has_enabled_keys():
                        continue
                    raise last_error

                shared_cooldown = await shared_credential_cooldown_seconds(
                    limiter,
                    provider=_PROVIDER,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                )
                if shared_cooldown > 0:
                    release_route()
                    pool.sync_cooldown(api_key, shared_cooldown)
                    if pool.healthy_key_count() > 0:
                        continue
                    raise last_error or TemplateCutError(
                        "PROVIDER_BUSY",
                        "模板分栏凭证暂时限流，请稍后重试",
                        retryable=True,
                    )

                rate_budget_reservation = await _acquire_template_rate_budget(
                    limiter=limiter,
                    route=template_route,
                    request_json=request_json,
                )
                response = await _post_with_shared_lease(
                    client=client,
                    limiter=limiter,
                    api_key=api_key,
                    base_url=base_url,
                    pool=pool,
                    teacher_id=teacher_id,
                    request_json=request_json,
                    route=template_route,
                    rate_budget_reservation=rate_budget_reservation,
                )
                status = response.status_code
                if status in {401, 403}:
                    release_route()
                    pool.mark_invalid(api_key, status)
                    await limiter.mark_credential_invalid(
                        provider=_PROVIDER,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        status_code=status,
                    )
                    last_error = TemplateCutError(
                        "CREDENTIAL_UNAVAILABLE",
                        "模板分栏凭证均不可用",
                    )
                    if pool.has_enabled_keys():
                        continue
                    raise last_error

                if status in {408, 429} or status >= 500:
                    retry_after = _retry_after_seconds(response)
                    release_route()
                    transient_attempt += 1
                    capability_failure_observed = (
                        capability_failure_observed or status != 429
                    )
                    cooldown = 0.0
                    if status == 429:
                        cooldown = pool.mark_429(api_key, retry_after)
                        await mark_shared_credential_cooldown(
                            limiter,
                            provider=_PROVIDER,
                            api_key=api_key,
                            base_url=base_url,
                            model=model,
                            cooldown_seconds=cooldown,
                        )
                    has_alternate_key = bool(
                        status == 429 and pool.healthy_key_count() > 0
                    )
                    last_error = TemplateCutError(
                        "PROVIDER_REJECTED",
                        "模板分栏服务暂时不可用",
                        retryable=True,
                    )
                    if transient_attempt < _MAX_TRANSIENT_ATTEMPTS:
                        retry_ceiling = (
                            0.5 if has_alternate_key else _RETRY_MAX_DELAY_S
                        )
                        retry_delay = exponential_retry_delay(
                            transient_attempt,
                            base_delay_s=(
                                _POOL_FAILOVER_DELAY_S
                                if has_alternate_key
                                else _RETRY_DELAY_S
                            ),
                            retry_after_s=(
                                None
                                if has_alternate_key
                                else (cooldown or retry_after)
                            ),
                            ceiling_s=retry_ceiling,
                        )
                        if not has_alternate_key and retry_delay > retry_ceiling:
                            if status != 429:
                                await _mark_template_capability_failure(limiter)
                            raise last_error
                        await asyncio.sleep(retry_delay)
                    continue

                if status != 200:
                    release_route()
                    if status in {404, 405}:
                        await _mark_template_capability_failure(limiter)
                    raise TemplateCutError(
                        "PROVIDER_REJECTED",
                        "模板分栏服务拒绝了本次请求",
                    )

                try:
                    body = response.json()
                    choices = body.get("choices") if isinstance(body, dict) else None
                    message = (
                        choices[0].get("message")
                        if isinstance(choices, list)
                        and choices
                        and isinstance(choices[0], dict)
                        else None
                    )
                    text = _extract_json_text(message)
                    payload = _strict_payload(json.loads(text))
                except (
                    TemplateCutError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ):
                    release_route()
                    if mode == "triple" and explicit_mode:
                        # A malformed or geometrically unsafe proposal is a
                        # review draft, not a reason to send the same unsafe
                        # crop through a retry loop.  The reservation/client
                        # cleanup still runs in the enclosing ``finally``.
                        return _review_layout_result(
                            source,
                            mode="triple",
                            diagnostics={
                                "strategy": "model_boundary_proposal",
                                "validation": "failed",
                                "confidence": 0.0,
                                "confidence_level": "low",
                                "reasons": ["provider_boundary_rejected"],
                            },
                            layout_revision=layout_revision,
                            previous_layout=previous_layout,
                            provider=_PROVIDER,
                        )
                    transient_attempt += 1
                    capability_failure_observed = True
                    last_error = TemplateCutError(
                        "PROVIDER_DATA_INVALID",
                        "模板分栏返回了无效 JSON",
                        retryable=True,
                    )
                    if transient_attempt < _MAX_TRANSIENT_ATTEMPTS:
                        await asyncio.sleep(
                            exponential_retry_delay(
                                transient_attempt,
                                base_delay_s=_RETRY_DELAY_S,
                                ceiling_s=_RETRY_MAX_DELAY_S,
                            )
                        )
                    continue
                pool.mark_success(api_key)
                release_route()
                await _clear_template_capability_failure(limiter)
                break
            except asyncio.CancelledError:
                release_route()
                raise
            except (httpx.TransportError, TimeoutError) as exc:
                release_route()
                transient_attempt += 1
                capability_failure_observed = True
                last_error = TemplateCutError(
                    "PROVIDER_REQUEST_FAILED",
                    "模板分栏请求失败，请检查网络或服务额度",
                    retryable=True,
                )
                if transient_attempt < _MAX_TRANSIENT_ATTEMPTS:
                    await asyncio.sleep(
                        exponential_retry_delay(
                            transient_attempt,
                            base_delay_s=_RETRY_DELAY_S,
                            ceiling_s=_RETRY_MAX_DELAY_S,
                        )
                    )
            except TemplateCutError:
                release_route()
                raise
            except Exception as exc:
                release_route()
                raise TemplateCutError(
                    "PROVIDER_REQUEST_FAILED",
                    "模板分栏请求失败，请检查网络或服务额度",
                    retryable=True,
                ) from exc

        if payload is None:
            if capability_failure_observed:
                await _mark_template_capability_failure(limiter)
            raise last_error or TemplateCutError(
                "PROVIDER_REQUEST_FAILED",
                "模板分栏请求失败，请检查网络或服务额度",
                retryable=True,
            )
    finally:
        request_json = None
        data_url = None
        response = None
        try:
            if owned_client:
                await client.aclose()
        finally:
            await payload_reservation.__aexit__(None, None, None)

    assert payload is not None
    if mode == "triple":
        # The model is only a proposer for triple layouts.  Re-run the explicit
        # geometry/evidence gate after the transport contract has succeeded.
        try:
            validated_columns = validate_layout_boundaries(
                payload["column_layout"]["columns"],
                column_mode="triple",
                evidence=payload,
            )
        except TemplateCutError as exc:
            return _review_layout_result(
                source,
                mode="triple",
                diagnostics={
                    "strategy": "model_boundary_proposal",
                    "validation": "failed",
                    "confidence": 0.0,
                    "confidence_level": "low",
                    "reasons": ["provider_boundary_rejected", exc.code],
                },
                layout_revision=layout_revision,
                previous_layout=previous_layout,
                proposed_columns=payload.get("column_layout", {}).get("columns"),
                provider=_PROVIDER,
            )
        payload["column_layout"]["columns"] = validated_columns
        # Geometry alone cannot prove three real columns: every column must
        # carry printed content and every seam must be a blank valley. The
        # evidence record (with its thresholds) is persisted either way.
        triple_evidence = await _run_local_ocr_image_component(
            _inspect_local_triple_evidence,
            source["path"],
            validated_columns,
        )
        # 像素证据不再整包否决：只有“提议栏内没有任何印刷内容”（模型编造栏）
        # 仍回退教师复核；seam/crossing 类证据降级为诊断记录（2026-10-03 合同：
        # 实测真值对照 qwen-column-split-20261003，边界落在成对印刷细线之间的
        # 空白通道属正常分栏，旧 seam_not_blank 会把合格边界误拒）。
        blocking_reasons = [
            reason
            for reason in (triple_evidence.get("reasons") or [])
            if str(reason).startswith("insufficient_column_content")
        ]
        if blocking_reasons:
            return _review_layout_result(
                source,
                mode="triple",
                diagnostics={
                    **triple_evidence,
                    "strategy": "model_boundary_proposal",
                    "gate_role": "diagnostics_only",
                    "reasons": [
                        "provider_boundary_rejected",
                        *list(blocking_reasons),
                    ],
                },
                layout_revision=layout_revision,
                previous_layout=previous_layout,
                proposed_columns=validated_columns,
                provider=_PROVIDER,
            )
        return _build_layout_result(
            source,
            payload,
            provider=_PROVIDER,
            request_id=str(body.get("id") or "") if isinstance(body, dict) else "",
            diagnostics={
                **triple_evidence,
                "strategy": "model_boundary_proposal",
                "gate_role": "diagnostics_only",
            },
            column_mode="triple",
            layout_revision=layout_revision,
            previous_layout=previous_layout,
        )
    if explicit_mode and mode == "double":
        try:
            validated_columns = validate_layout_boundaries(
                payload["column_layout"]["columns"],
                column_mode="double",
                evidence=payload,
            )
        except TemplateCutError as exc:
            return _review_layout_result(
                source,
                mode="double",
                diagnostics={
                    **(double_local_evidence or {}),
                    "strategy": "model_boundary_proposal",
                    "gate_role": "diagnostics_only",
                    "validation": "failed",
                    "confidence": 0.0,
                    "confidence_level": "low",
                    "reasons": ["provider_boundary_rejected", exc.code],
                },
                layout_revision=layout_revision,
                previous_layout=previous_layout,
                proposed_columns=double_local_columns,
                provider=_PROVIDER,
            )
        payload["column_layout"]["columns"] = validated_columns
        return _build_layout_result(
            source,
            payload,
            provider=_PROVIDER,
            request_id=str(body.get("id") or "") if isinstance(body, dict) else "",
            diagnostics={
                **(double_local_evidence or {}),
                "strategy": "model_boundary_proposal",
                "gate_role": "diagnostics_only",
            },
            column_mode="double",
            layout_revision=layout_revision,
            previous_layout=previous_layout,
        )
    return _build_layout_result(
        source,
        payload,
        provider=_PROVIDER,
        request_id=str(body.get("id") or "") if isinstance(body, dict) else "",
        column_mode=(mode if explicit_mode else None),
        layout_revision=layout_revision,
        previous_layout=previous_layout,
    )
