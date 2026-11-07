"""What every part of the accelerator job service shares: its configuration, stage names and small helpers.

- Configuration read from the environment at import, checked at import:
  ``ALGORITHM_VERSION``, ``LEASE_SECONDS``, ``MAX_ATTEMPTS``,
  ``NODE_STALE_SECONDS``, ``JOB_DEADLINE_SECONDS``, ``NODE_MAX_ACTIVE_LEASES``
  and ``CPU_FALLBACK_STALE_SECONDS``; an out-of-range value fails the import.
- Stage names and the vision model contract versions:
  ``TEMPLATE_OCR_EVIDENCE_STAGE``, ``STUDENT_MULTI_EVIDENCE_STAGE``,
  ``VISION_MODEL_STAGE``, ``VISION_MODEL_REQUEST_CONTRACT_VERSION``,
  ``VISION_MODEL_RESPONSE_CONTRACT_VERSION``.
- ``utcnow``, ``canonical_json``, ``sha256_file``, ``sha256_json``.

Moved unchanged from services/accelerator_jobs.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALGORITHM_VERSION = os.environ.get("ACCELERATOR_ALGORITHM_VERSION", "template-registration-v2")
LEASE_SECONDS = int(os.environ.get("ACCELERATOR_LEASE_SECONDS", "90"))
MAX_ATTEMPTS = int(os.environ.get("ACCELERATOR_MAX_ATTEMPTS", "2"))
NODE_STALE_SECONDS = int(os.environ.get("ACCELERATOR_NODE_STALE_SECONDS", "30"))
JOB_DEADLINE_SECONDS = int(os.environ.get("ACCELERATOR_JOB_DEADLINE_SECONDS", "600"))
NODE_MAX_ACTIVE_LEASES = int(os.environ.get("ACCELERATOR_NODE_MAX_ACTIVE_LEASES", "48"))
CPU_FALLBACK_STALE_SECONDS = JOB_DEADLINE_SECONDS
TEMPLATE_OCR_EVIDENCE_STAGE = "template_ocr_evidence"


STUDENT_MULTI_EVIDENCE_STAGE = "student_multi_evidence"
VISION_MODEL_STAGE = "vision_model_request"
VISION_MODEL_REQUEST_CONTRACT_VERSION = "vision-model-request-v1"
VISION_MODEL_RESPONSE_CONTRACT_VERSION = "vision-model-response-v1"


if not 30 <= LEASE_SECONDS <= 300:
    raise RuntimeError("ACCELERATOR_LEASE_SECONDS must be between 30 and 300")
if not 1 <= MAX_ATTEMPTS <= 5:
    raise RuntimeError("ACCELERATOR_MAX_ATTEMPTS must be between 1 and 5")
if not 60 <= JOB_DEADLINE_SECONDS <= 3600:
    raise RuntimeError("ACCELERATOR_JOB_DEADLINE_SECONDS must be between 60 and 3600")
if not 1 <= NODE_MAX_ACTIVE_LEASES <= 64:
    raise RuntimeError("ACCELERATOR_NODE_MAX_ACTIVE_LEASES must be between 1 and 64")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
