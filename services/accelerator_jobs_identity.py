"""What identifies a job's input: reusable template OCR evidence and a normalized vision model layout.

- Template OCR evidence is reused across imports of the same immutable page:
  ``normalize_template_ocr_question_schema`` bounds the question schema,
  ``template_ocr_identity_payload`` and ``template_ocr_identity`` derive the
  stable identity (``TEMPLATE_OCR_IDENTITY_VERSION``),
  ``template_ocr_layout_identity`` derives it from a normalized layout, and
  ``template_ocr_cache_root`` / ``template_ocr_cache_path`` locate the
  server-owned cache.
- ``_normalize_vision_model_layout`` validates the layout snapshot of a vision
  model request against its source and template files and hashes, within the
  prompt, asset and provider-profile limits.

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from services.accelerator_artifacts import TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION
from services.accelerator_jobs_base import (
    ALGORITHM_VERSION,
    VISION_MODEL_REQUEST_CONTRACT_VERSION,
    canonical_json,
    sha256_file,
    sha256_json,
)


# Stable identity for reusable template-exemption OCR evidence.  This is kept
# separate from the per-request operation id so a later import of the same
# immutable template page can reuse an accepted montage.
TEMPLATE_OCR_IDENTITY_VERSION = "template-ocr-identity-v1"
TEMPLATE_OCR_CACHE_DIR_NAME = ".template-ocr-cache"


_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TEMPLATE_OCR_IDENTITY_RE = re.compile(r"[0-9a-f]{64}\Z")


_MAX_MODEL_PROMPT_BYTES = 64 * 1024


_MAX_MODEL_ASSETS = 3
_MODEL_PROVIDER_PROFILES = frozenset({
    "primary",
    "failover",
    "accelerator",
    "retry",
})


def normalize_template_ocr_question_schema(value: Any) -> list[dict[str, Any]]:
    """Return a deterministic, bounded question schema for template OCR jobs.

    The worker only needs the crop plan, but the schema is part of the business
    identity: changing question order/type/geometry must never silently reuse a
    montage produced for an earlier template contract.  Keep the payload JSON
    shaped and preserve additional schema fields so future contract revisions
    naturally produce a different identity.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("template OCR question schema is invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            item = {"question_number": str(item).strip()}
        if not isinstance(item, dict):
            raise ValueError("template OCR question schema entries are invalid")
        try:
            entry = json.loads(canonical_json(item))
        except (TypeError, ValueError) as exc:
            raise ValueError("template OCR question schema is not JSON safe") from exc
        if not isinstance(entry, dict):
            raise ValueError("template OCR question schema entry is invalid")
        number = str(entry.get("question_number") or "").strip()
        if not number or len(number) > 64 or any(ord(char) < 32 for char in number):
            raise ValueError("template OCR question number is invalid")
        if number in seen:
            raise ValueError("template OCR question schema contains duplicates")
        seen.add(number)
        entry["question_number"] = number
        normalized.append(entry)
    # Sorting is intentionally not used: question order is part of the frozen
    # schema and therefore part of the cache identity.
    if len(canonical_json(normalized).encode("utf-8")) > 64 * 1024:
        raise ValueError("template OCR question schema is too large")
    return normalized


def template_ocr_identity_payload(
    *,
    teacher_id: int,
    canonical_sha256: str,
    question_crop_plan: dict[str, Any],
    page_index: int,
    question_schema: Any = None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> dict[str, Any]:
    """Build the fields that define reusable template OCR evidence."""
    owner_id = int(teacher_id)
    source_sha = str(canonical_sha256 or "").strip().lower()
    if owner_id < 1 or not _SHA256_RE.fullmatch(source_sha):
        raise ValueError("template OCR identity source is invalid")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or not 0 <= page_index <= 10_000:
        raise ValueError("template OCR identity page index is invalid")
    if not isinstance(question_crop_plan, dict):
        raise ValueError("template OCR identity crop plan is invalid")
    # Import lazily to keep the job protocol usable by lightweight node tools.
    from services.template_anchor_crops import validate_anchor_crop_plan

    normalized_plan = validate_anchor_crop_plan(
        question_crop_plan,
        artifact_sha256=source_sha,
    )
    if normalized_plan != question_crop_plan:
        raise ValueError("template OCR identity crop plan is not canonical")
    normalized_schema = normalize_template_ocr_question_schema(question_schema)
    return {
        "identity_version": TEMPLATE_OCR_IDENTITY_VERSION,
        "teacher_id": owner_id,
        "canonical_sha256": source_sha,
        "question_crop_plan": normalized_plan,
        "question_schema": normalized_schema,
        "page_index": page_index,
        "contract_version": TEMPLATE_OCR_EVIDENCE_CONTRACT_VERSION,
        "algorithm_version": str(algorithm_version or ALGORITHM_VERSION),
    }


def template_ocr_identity(
    *,
    teacher_id: int,
    canonical_sha256: str,
    question_crop_plan: dict[str, Any],
    page_index: int,
    question_schema: Any = None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    return sha256_json(template_ocr_identity_payload(
        teacher_id=teacher_id,
        canonical_sha256=canonical_sha256,
        question_crop_plan=question_crop_plan,
        page_index=page_index,
        question_schema=question_schema,
        algorithm_version=algorithm_version,
    ))


def template_ocr_cache_root(source_path: str | Path) -> Path:
    """Resolve the server-owned cache root shared by template sessions."""
    configured = os.environ.get("ACCELERATOR_TEMPLATE_OCR_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    source = Path(source_path).resolve()
    # Uploaded canonical pages normally live below ``template_<id>``.  Use its
    # parent so independent template operations share one cache namespace.
    for ancestor in (source.parent, *source.parents):
        if ancestor.name.startswith("template_"):
            return (ancestor.parent / TEMPLATE_OCR_CACHE_DIR_NAME).resolve()
    return (source.parent / TEMPLATE_OCR_CACHE_DIR_NAME).resolve()


def template_ocr_cache_path(
    source_path: str | Path,
    *,
    teacher_id: int,
    identity: str,
) -> Path:
    owner_id = int(teacher_id)
    if owner_id < 1 or not _TEMPLATE_OCR_IDENTITY_RE.fullmatch(str(identity or "")):
        raise ValueError("template OCR cache identity is invalid")
    root = template_ocr_cache_root(source_path)
    path = (root / f"teacher_{owner_id}" / str(identity) / "evidence.png").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("template OCR cache path escaped its root") from exc
    return path


def template_ocr_layout_identity(
    *,
    teacher_id: int,
    source_sha256: str,
    layout_snapshot: dict[str, Any],
    algorithm_version: str = ALGORITHM_VERSION,
) -> str | None:
    """Derive a stable identity from a normalized template OCR layout."""
    if not isinstance(layout_snapshot, dict):
        return None
    if layout_snapshot.get("identity_version") != TEMPLATE_OCR_IDENTITY_VERSION:
        return None
    try:
        return template_ocr_identity(
            teacher_id=teacher_id,
            canonical_sha256=source_sha256,
            question_crop_plan=layout_snapshot["question_crop_plan"],
            page_index=layout_snapshot["page_index"],
            question_schema=layout_snapshot.get("question_schema"),
            algorithm_version=algorithm_version,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _normalize_vision_model_layout(
    raw: Any,
    *,
    source: Path,
    template: Path,
    source_sha256: str,
    template_sha256: str,
) -> dict[str, Any]:
    required_keys = {
        "contract_version",
        "request_digest",
        "provider_profile",
        "model",
        "prompt",
        "max_output_tokens",
        "reasoning_effort",
        "stream",
        "first_event_timeout_s",
        "attempt_ttl_s",
        "sse_idle_timeout_s",
        "assets",
    }
    optional_keys = {"prompt_first"}
    if (
        not isinstance(raw, dict)
        or not required_keys.issubset(raw)
        or set(raw) - required_keys - optional_keys
    ):
        raise ValueError("vision model request has invalid fields")
    prompt_first = raw.get("prompt_first", False)
    if not isinstance(prompt_first, bool):
        raise ValueError("vision model prompt_first is invalid")
    if raw.get("contract_version") != VISION_MODEL_REQUEST_CONTRACT_VERSION:
        raise ValueError("vision model request contract is unsupported")
    profile = str(raw.get("provider_profile") or "").strip()
    if profile not in _MODEL_PROVIDER_PROFILES:
        raise ValueError("vision model provider profile is invalid")
    model = str(raw.get("model") or "").strip()
    if not _IDENTIFIER_RE.fullmatch(model):
        raise ValueError("vision model name is invalid")
    prompt = raw.get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt.encode("utf-8")) > _MAX_MODEL_PROMPT_BYTES
    ):
        raise ValueError("vision model prompt is invalid")
    max_output_tokens = raw.get("max_output_tokens")
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 25_000 <= max_output_tokens <= 32_768
    ):
        raise ValueError("vision model output limit is invalid")
    if raw.get("reasoning_effort") != "high" or raw.get("stream") is not True:
        raise ValueError("vision model reasoning or stream contract is invalid")

    timeouts: dict[str, float] = {}
    timeout_ranges = {
        "first_event_timeout_s": (5.0, 180.0),
        "attempt_ttl_s": (30.0, 600.0),
        "sse_idle_timeout_s": (10.0, 300.0),
    }
    for field, (minimum, maximum) in timeout_ranges.items():
        value = raw.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise ValueError(f"vision model {field} is invalid")
        timeouts[field] = float(value)
    if (
        timeouts["first_event_timeout_s"] > timeouts["attempt_ttl_s"]
        or timeouts["sse_idle_timeout_s"] > timeouts["attempt_ttl_s"]
    ):
        raise ValueError("vision model timeout ordering is invalid")

    assets = raw.get("assets")
    if not isinstance(assets, list) or not 1 <= len(assets) <= _MAX_MODEL_ASSETS:
        raise ValueError("vision model assets are invalid")
    task_root = source.parent.resolve()
    if not task_root.name.startswith(".vision-model-"):
        raise ValueError("vision model asset directory is invalid")
    normalized_assets: list[dict[str, Any]] = []
    for position, item in enumerate(assets, 1):
        kind = f"input_{position:02d}"
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "sha256",
            "byte_size",
            "media_type",
        }:
            raise ValueError("vision model asset metadata is invalid")
        if item.get("kind") != kind or not _is_sha256(item.get("sha256")):
            raise ValueError("vision model asset identity is invalid")
        byte_size = item.get("byte_size")
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or not 1 <= byte_size <= 25 * 1024 * 1024
        ):
            raise ValueError("vision model asset size is invalid")
        media_type = item.get("media_type")
        if media_type not in {"image/png", "image/jpeg"}:
            raise ValueError("vision model asset media type is invalid")
        asset_path = (task_root / f"{kind}.bin").resolve()
        if (
            asset_path.parent != task_root
            or not asset_path.is_file()
            or asset_path.stat().st_size != byte_size
            or sha256_file(asset_path) != item["sha256"]
        ):
            raise ValueError("vision model asset changed or is missing")
        normalized_assets.append({
            "kind": kind,
            "sha256": item["sha256"],
            "byte_size": byte_size,
            "media_type": media_type,
        })

    first = (task_root / "input_01.bin").resolve()
    last = (task_root / f"input_{len(normalized_assets):02d}.bin").resolve()
    if (
        source != first
        or template != last
        or source_sha256 != normalized_assets[0]["sha256"]
        or template_sha256 != normalized_assets[-1]["sha256"]
    ):
        raise ValueError("vision model anchor assets are inconsistent")

    normalized = {
        "contract_version": VISION_MODEL_REQUEST_CONTRACT_VERSION,
        "request_digest": str(raw.get("request_digest") or "").strip(),
        "provider_profile": profile,
        "model": model,
        "prompt": prompt,
        "max_output_tokens": max_output_tokens,
        "reasoning_effort": "high",
        "stream": True,
        **timeouts,
        "assets": normalized_assets,
    }
    if "prompt_first" in raw:
        normalized["prompt_first"] = prompt_first
    digest_payload = dict(normalized)
    request_digest = digest_payload.pop("request_digest")
    if not _is_sha256(request_digest) or sha256_json(digest_payload) != request_digest:
        raise ValueError("vision model request digest is invalid")
    return normalized
