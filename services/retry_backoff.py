"""Small positive jitter for bounded retry delays."""

from __future__ import annotations

import math
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


def parse_retry_after_seconds(
    value: str | float | int | None,
    *,
    now_epoch: float | None = None,
) -> float | None:
    """Parse Retry-After delta-seconds or an HTTP-date."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = retry_at.timestamp() - (
            time.time() if now_epoch is None else float(now_epoch)
        )
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def exponential_retry_delay(
    attempt: int,
    *,
    base_delay_s: float,
    retry_after_s: float | None = None,
    ceiling_s: float = 5.0,
) -> float:
    """Return capped exponential backoff plus jitter for one retry number."""
    if attempt < 1:
        raise ValueError("retry attempt must be positive")
    base_delay = float(base_delay_s)
    ceiling = float(ceiling_s)
    if not math.isfinite(base_delay) or base_delay <= 0:
        raise ValueError("base retry delay must be finite and positive")
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError("retry delay ceiling must be finite and positive")
    delay = base_delay * (2 ** (attempt - 1))
    if (
        retry_after_s is not None
        and math.isfinite(retry_after_s)
        and retry_after_s > 0
    ):
        if retry_after_s >= ceiling:
            # Retry-After is a provider contract, not a hint we may shorten.
            # Callers that cannot keep a workflow open this long must fail fast
            # and defer the credential instead of retrying early.
            return retry_after_s
        delay = max(delay, retry_after_s)
    return jittered_backoff(delay, ceiling_s=ceiling)


def jittered_backoff(
    delay_s: float,
    *,
    jitter_ratio: float = 0.25,
    max_jitter_s: float = 1.0,
    ceiling_s: float | None = None,
) -> float:
    """Return ``delay_s`` plus bounded jitter without retrying early."""
    delay = float(delay_s)
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("retry delay must be finite and nonnegative")
    if not math.isfinite(jitter_ratio) or jitter_ratio < 0:
        raise ValueError("jitter ratio must be finite and nonnegative")
    if not math.isfinite(max_jitter_s) or max_jitter_s < 0:
        raise ValueError("maximum jitter must be finite and nonnegative")
    if ceiling_s is not None:
        ceiling = float(ceiling_s)
        if not math.isfinite(ceiling) or ceiling < 0:
            raise ValueError("retry delay ceiling must be finite and nonnegative")
        delay = min(delay, ceiling)
        headroom = max(0.0, ceiling - delay)
    else:
        headroom = math.inf

    jitter = min(delay * jitter_ratio, max_jitter_s, headroom)
    if jitter <= 0:
        return delay
    return delay + random.uniform(0.0, jitter)
