"""Compatibility helpers for legacy normalized scores and v2 category semantics.

v1 rows stored every batch as a 0..1 score.  v2 rows store actual points only
for exams and store correctness separately for daily work.  Consumers
must use these helpers instead of guessing from the magnitude of ``score``.
"""

from __future__ import annotations

import math
from typing import Any


def _value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _finite(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def record_correct_rate(record: Any) -> float | None:
    """Return a comparable 0..1 correctness rate for v1 and v2 records."""
    explicit = _finite(_value(record, "correct_rate"))
    if explicit is not None:
        return min(1.0, max(0.0, explicit))

    score = _finite(_value(record, "score"))
    if score is None:
        return None

    version = int(_finite(_value(record, "scoring_version")) or 1)
    category = str(_value(record, "category", "") or "").lower()
    max_score = _finite(_value(record, "max_score"))
    if version >= 2:
        if category == "exam" and max_score and max_score > 0:
            return min(1.0, max(0.0, score / max_score))
        return None

    # Historical quick-grade rows always used a normalized score.  A few manual
    # imports stored 0..100; preserve those defensively.
    ratio = score / 100.0 if score > 1 else score
    return min(1.0, max(0.0, ratio))


def exam_points(record: Any) -> tuple[float | None, float | None]:
    """Return (awarded, maximum) for an exam, including v1 100-point fallback."""
    if str(_value(record, "category", "") or "").lower() != "exam":
        return None, None
    score = _finite(_value(record, "score"))
    if score is None:
        return None, _finite(_value(record, "max_score"))

    version = int(_finite(_value(record, "scoring_version")) or 1)
    maximum = _finite(_value(record, "max_score"))
    if version >= 2:
        return score, maximum
    if score <= 1:
        return score * 100.0, 100.0
    return score, maximum


def display_metric(record: Any) -> dict[str, Any]:
    """Return an explicit display contract; never infer semantics from score size."""
    category = str(_value(record, "category", "") or "daily").lower()
    if category == "exam":
        awarded, maximum = exam_points(record)
        return {"kind": "points", "value": awarded, "maximum": maximum}
    return {"kind": "correct_rate", "value": record_correct_rate(record), "maximum": None}
