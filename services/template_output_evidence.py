"""Private, bounded template model-output receipts; never public raw logs.

Only failed template outputs are retained. No images, prompts, HTTP headers,
credentials or reasoning are recorded. Ordinary processing logs carry opaque
receipt IDs; operators use the correlation ID to find this protected file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

logger = logging.getLogger(__name__)
EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "logs" / "template-output-failures"
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 32
RETENTION_SECONDS = 36 * 3600
_lock = threading.Lock()
_ID = re.compile(r"[a-f0-9]{32}\Z")
_QUESTION = re.compile(r"Q?[1-9][0-9]{0,3}(?:\.[1-9][0-9]{0,3})?\Z")
_FIELDS = {"contract_version", "coordinate_space", "questions", "answers", "needs_review", "q_id",
           "question_number", "printed_path", "slots", "slot_id", "cue", "bbox", "slot_bbox",
           "question_number_bbox", "question_type", "choice_mode", "regions", "kind", "label_anchor",
           "question_text", "uncertain", "standard_answer", "answer_items", "answer", "solution_steps", "visual_facts"}


def _redact(text: str) -> str:
    # Known credential-like strings are never written even if a model echoes them.
    text = re.sub(r"(?i)\b(?:sk-|key-)[A-Za-z0-9_-]{8,}", "[credential-redacted]", text)
    return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text)


def analyze_output(text: str, *, expected_ids=()) -> dict:
    """Describe shape/identity without printing question text or answers."""
    stripped = text.strip()
    if stripped.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", stripped)
        if match:
            stripped = match.group(1)
    duplicate_keys = []

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                duplicate_keys.append(key if key in _FIELDS else "[unknown]")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(stripped, object_pairs_hook=pairs, parse_constant=invalid)
    except json.JSONDecodeError as exc:
        return {"json_valid": False, "reason": "json_syntax", "line": exc.lineno, "column": exc.colno}
    except (ValueError, RecursionError):
        return {"json_valid": False, "reason": "invalid_json_value"}
    result = {"json_valid": not duplicate_keys, "top_level_type": type(value).__name__,
              "duplicate_keys": duplicate_keys[:32]}
    if isinstance(value, dict):
        result["top_level_fields"] = sorted(key if key in _FIELDS else "[unknown]" for key in value)
        rows = value.get("questions", value.get("answers"))
    else:
        rows = value if isinstance(value, list) else None
    if isinstance(rows, list):
        result["row_count"] = len(rows)
        result["row_types"] = sorted({type(row).__name__ for row in rows})
        identities = [row.get("q_id", row.get("question_number")) for row in rows if isinstance(row, dict)]
        identities = [i for i in identities if isinstance(i, str) and _QUESTION.fullmatch(i)]
        result["question_ids"] = identities[:200]
        result["duplicate_ids"] = sorted({i for i in identities if identities.count(i) > 1})[:200]
        if expected_ids:
            result["missing_ids"] = sorted(set(expected_ids) - set(identities))[:200]
            result["unexpected_ids"] = sorted(set(identities) - set(expected_ids))[:200]
        result["row_fields"] = [sorted(key if key in _FIELDS else "[unknown]" for key in row)
                                for row in rows[:200] if isinstance(row, dict)]
    return result


def _save_output_failure(error, *, error_info: dict, phase: str, model: str, route: str,
                        prompt: str, content: str | None = None, expected_ids=(), trace_id="none") -> str | None:
    """Persist the failed final text, never alter the original failure if I/O fails.

    Retention is bounded by count and age on each write (not a new cron job).
    Raw content lives only here, outside all static/upload serving roots.
    """
    original = error
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        captured = getattr(error, "_model_output_text", None)
        if isinstance(captured, str):
            content = captured
            break
        error = error.__cause__ or error.__context__
    if not isinstance(content, str):
        return None
    receipt_id = error_info.get("error_id", "")
    cid = error_info.get("correlation_id", "")
    if not _ID.fullmatch(receipt_id) or not _ID.fullmatch(cid):
        return None
    raw_bytes = content.encode("utf-8")
    kept = raw_bytes[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    private_message = getattr(error, "_output_error_message", str(original))
    packet = {
        "schema_version": 1, "receipt_id": receipt_id, "correlation_id": cid,
        "created_at_unix": time.time(), "phase": phase, "model": model, "route": route,
        "trace_id": trace_id, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "content_sha256": hashlib.sha256(raw_bytes).hexdigest(), "content_bytes": len(raw_bytes),
        "content_truncated": len(raw_bytes) > MAX_CONTENT_BYTES,
        "content": _redact(kept), "analysis": analyze_output(kept, expected_ids=expected_ids),
        "expected_question_ids": list(expected_ids),
        "validation_error_type": getattr(error, "_output_error_type", type(original).__name__),
        "validation_error": _redact(private_message)[:2000],
    }
    try:
        with _lock:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            if EVIDENCE_DIR.is_symlink():
                raise OSError("refusing symlink evidence directory")
            os.chmod(EVIDENCE_DIR, 0o700)
            files = sorted((p for p in EVIDENCE_DIR.glob("*.json") if _ID.fullmatch(p.stem)
                            and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
            cutoff = time.time() - RETENTION_SECONDS
            for path in files:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            files = [p for p in files if p.exists()]
            for path in files[:max(0, len(files) - MAX_RECEIPTS + 1)]:
                path.unlink(missing_ok=True)
            path = EVIDENCE_DIR / (receipt_id + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(packet, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        return receipt_id
    except (OSError, ValueError, TypeError, RecursionError):
        logger.warning("Template output evidence could not be saved; original failure preserved")
        return None


def save_output_failure(error, **kwargs) -> str | None:
    """Diagnostics must never replace the business exception, even on bad text."""
    try:
        return _save_output_failure(error, **kwargs)
    except Exception:
        logger.warning("Template output evidence could not be prepared; original failure preserved")
        return None
