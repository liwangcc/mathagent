"""The per-request context nested OCR calls read: who asks, which route, which phase, which batch.

Context variables let the low-level vision calls join the shared fair limiter
and the batch deadline without widening every caller's signature:
``set_vision_teacher_id``, ``set_vision_route_ordinal``, ``set_vision_phase_key``
(the deadline and retry-ledger scopes of one logical phase),
``set_vision_batch_paper_count`` and ``set_vision_batch_deadline_s`` each set one
and return the token(s) the matching ``reset_*`` function takes.

Moved unchanged from services/ocr_service.py, which still exposes these names.
"""

import math
from contextvars import ContextVar


# The context variable lets low-level call_vision participate in the shared
# fair limiter without widening every template-recognition function signature.
_vision_teacher_id: ContextVar[str] = ContextVar("vision_teacher_id", default="system")
_vision_route_ordinal: ContextVar[int | None] = ContextVar("vision_route_ordinal", default=None)
_vision_phase_key: ContextVar[str] = ContextVar("vision_phase_key", default="")
_vision_retry_phase_key: ContextVar[str] = ContextVar(
    "vision_retry_phase_key", default=""
)
_vision_batch_paper_count: ContextVar[int | None] = ContextVar(
    "vision_batch_paper_count", default=None
)
_vision_batch_deadline_s: ContextVar[float | None] = ContextVar(
    "vision_batch_deadline_s", default=None
)


def set_vision_teacher_id(teacher_id: str):
    """Set request-local teacher context for every low-level Vision call."""
    return _vision_teacher_id.set(str(teacher_id or "system"))


def reset_vision_teacher_id(token) -> None:
    _vision_teacher_id.reset(token)


def set_vision_route_ordinal(ordinal: int):
    return _vision_route_ordinal.set(int(ordinal))


def reset_vision_route_ordinal(token) -> None:
    _vision_route_ordinal.reset(token)


def set_vision_phase_key(phase_key: str, *, retry_scope_key: str | None = None):
    """Select deadline and retry-ledger scopes for one logical OCR phase."""
    normalized = str(phase_key or "").strip()
    if not normalized or len(normalized) > 240:
        raise ValueError("vision phase key must contain 1-240 characters")
    retry_scope = normalized if retry_scope_key is None else str(retry_scope_key).strip()
    if not retry_scope or len(retry_scope) > 240:
        raise ValueError("vision retry scope key must contain 1-240 characters")
    return (
        _vision_phase_key.set(normalized),
        _vision_retry_phase_key.set(retry_scope),
    )


def reset_vision_phase_key(token) -> None:
    phase_token, retry_token = token
    _vision_retry_phase_key.reset(retry_token)
    _vision_phase_key.reset(phase_token)


def set_vision_batch_paper_count(paper_count: int):
    """Attach one logical student-batch size to nested OCR requests."""
    if isinstance(paper_count, bool) or not isinstance(paper_count, int):
        raise ValueError("vision batch paper count must be a positive integer")
    if paper_count < 1:
        raise ValueError("vision batch paper count must be a positive integer")
    return _vision_batch_paper_count.set(paper_count)


def reset_vision_batch_paper_count(token) -> None:
    _vision_batch_paper_count.reset(token)


def set_vision_batch_deadline_s(deadline_s: float):
    """Override one workflow's scheduler deadline without changing pool limits."""
    normalized = float(deadline_s)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("vision batch deadline must be finite and positive")
    return _vision_batch_deadline_s.set(normalized)


def reset_vision_batch_deadline_s(token) -> None:
    _vision_batch_deadline_s.reset(token)
