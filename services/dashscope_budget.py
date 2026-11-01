"""Per-model DashScope pay-as-you-go rate budget.

Student OCR, template Flash and grading use the same qwen3.8-flash rolling
RPM/TPM window. The retired Plus model has no budget entry. A request reserves an
estimate of its billed tokens -- never Base64 or UTF-8 bytes -- and waits for
the window rather than failing after the short concurrency-lease timeout.

The estimate keeps the full output ceiling while a request is in flight, so
the window never admits more than the provider could be asked to bill. The
transport that sent the request then settles it exactly once to the reported
input+output usage, or refunds it when no bytes left this process. Missing,
conflicting or uncertain usage keeps the estimate until the window expires.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math

import httpx

from config import DASHSCOPE_MODEL_RATE_LIMITS, MODEL_RATE_BUDGET_WAIT_SECONDS

logger = logging.getLogger(__name__)

# Connection-phase failures: the provider never received request bytes, so the
# reservation may be refunded. Any later failure is ambiguous and is kept.
UNSENT_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Billed vision input does not grow with file size; a full exam page is
# observed at roughly 1.5K-3K prompt tokens, so reserve the upper bound.
IMAGE_TOKENS = 3_000


def _is_cjk(char: str) -> bool:
    # CJK symbols/ideographs, compatibility ideographs, full-width forms.
    return (
        "\u3000" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\uff00" <= char <= "\uffef"
    )


def text_tokens(text: str) -> int:
    """Over-estimate Qwen tokens: one per CJK character, one per 3 others."""
    cjk = sum(1 for char in text if _is_cjk(char))
    return cjk + math.ceil((len(text) - cjk) / 3)


def estimate_request_tokens(payload: dict, completion_tokens: int | None = None) -> int:
    """Reserve prompt text, a flat allowance per image, and the output ceiling."""
    if completion_tokens is None:
        try:
            completion_tokens = int(
                payload.get("max_completion_tokens") or payload.get("max_tokens") or 0
            )
        except (TypeError, ValueError):
            completion_tokens = 0
    text = 0
    images = 0

    def visit(value: object) -> None:
        nonlocal text, images
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            if value.startswith("data:image/"):
                images += 1
            else:
                text += text_tokens(value)

    visit(payload)
    return max(1, text + images * IMAGE_TOKENS + max(0, int(completion_tokens)))


def rate_limits(model: str) -> tuple[int, int]:
    try:
        return DASHSCOPE_MODEL_RATE_LIMITS[model]
    except KeyError:
        raise ValueError(f"DashScope model {model!r} has no declared rate budget") from None


def budget_provider(model: str) -> str:
    return f"dashscope:{model}"


def actual_usage_tokens(usage: object) -> int | None:
    """Return a provider-reported total token count, or ``None`` if unknown.

    DashScope-compatible responses have appeared with both OpenAI names
    (``prompt_tokens``/``completion_tokens``) and DashScope names
    (``input_tokens``/``output_tokens``).  Missing or contradictory usage is
    deliberately unknown: callers must keep the reservation rather than
    refunding tokens that may have been consumed.
    """
    if not isinstance(usage, dict):
        return None

    def integer(name: str) -> int | None:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def consistent(names: tuple[str, ...]) -> int | None:
        values: list[int] = []
        for name in names:
            if name not in usage:
                continue
            value = integer(name)
            if value is None:
                return None
            values.append(value)
        if len(set(values)) > 1:
            return None
        return values[0] if values else None

    total = integer("total_tokens") if "total_tokens" in usage else None
    if "total_tokens" in usage and total is None:
        return None
    input_tokens = consistent(("input_tokens", "prompt_tokens"))
    output_tokens = consistent(("output_tokens", "completion_tokens"))
    if total is not None:
        if (
            input_tokens is not None
            and output_tokens is not None
            and total != input_tokens + output_tokens
        ):
            return None
        return total
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


async def reserve(
    limiter,
    *,
    model: str,
    payload: dict,
    completion_tokens: int | None = None,
):
    """Reserve a provider window slot when the limiter supports settlement.

    Focused tests and legacy callers may provide only ``acquire_rate_budget``;
    those callers retain the old fire-and-forget behavior and receive
    ``None``.  Production limiters expose the reservation API below so callers
    can cancel a request that never reached HTTP and settle actual usage.
    """
    acquire_budget = getattr(limiter, "reserve_rate_budget", None)
    if not callable(acquire_budget):
        legacy_acquire = getattr(limiter, "acquire_rate_budget", None)
        if not callable(legacy_acquire):
            return None
        rpm, tpm = rate_limits(model)
        await legacy_acquire(
            provider=budget_provider(model),
            requests_per_minute=rpm,
            tokens_per_minute=tpm,
            reserved_tokens=min(tpm, estimate_request_tokens(payload, completion_tokens)),
            timeout_seconds=MODEL_RATE_BUDGET_WAIT_SECONDS,
        )
        return None
    rpm, tpm = rate_limits(model)
    return await acquire_budget(
        provider=budget_provider(model),
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        reserved_tokens=min(tpm, estimate_request_tokens(payload, completion_tokens)),
        timeout_seconds=MODEL_RATE_BUDGET_WAIT_SECONDS,
    )


def response_usage(response) -> object:
    """Return the final provider usage object from a JSON or SSE body.

    Only the usage-bearing tail of a stream is decoded; callers still parse
    content with their own strict contracts. A usage line that cannot be
    decoded makes usage unknown, never an earlier (possibly partial) value.
    """
    def object_usage(body: object) -> object:
        usage = body.get("usage") if isinstance(body, dict) else None
        return usage if isinstance(usage, dict) else None

    try:
        raw = response.text
    except (AttributeError, RuntimeError, UnicodeDecodeError):
        raw = None
    if not isinstance(raw, str) or not raw.strip():
        try:
            return object_usage(response.json())
        except Exception:
            return None
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            return object_usage(json.loads(stripped))
        except ValueError:
            return None
    # Usage normally arrives in the last event, after finish_reason; streams
    # that repeat ``"usage": null`` or cumulative usage resolve to the last
    # concrete object. A JSON-escaped quote can never produce '"usage"'.
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("data:") or '"usage"' not in line:
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return None
        usage = object_usage(event)
        if usage is not None:
            return usage
    return None


_missing_usage_logged = False


async def settle(limiter, reservation, usage: object) -> bool:
    """Settle a reservation from actual usage; unknown usage is not refunded.

    Coordinator failure keeps the conservative estimate until its window
    expires; it never fails a provider response that was already received.
    """
    global _missing_usage_logged
    if reservation is None:
        return False
    actual_tokens = actual_usage_tokens(usage)
    if actual_tokens is None:
        if not _missing_usage_logged:
            _missing_usage_logged = True
            logger.warning(
                "provider response had no usable token usage; "
                "rate reservation kept at its estimate"
            )
        return False
    settle_budget = getattr(limiter, "settle_rate_budget", None)
    if not callable(settle_budget):
        return False
    try:
        return bool(await asyncio.shield(
            settle_budget(reservation, actual_tokens=actual_tokens)
        ))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("failed to settle rate reservation; estimate kept", exc_info=True)
        return False


async def settle_response(limiter, reservation, response) -> bool:
    """Settle a received response from its own reported usage."""
    if reservation is None:
        return False
    return await settle(limiter, reservation, response_usage(response))


async def cancel(limiter, reservation) -> bool:
    """Cancel a reservation only when the request definitely was not sent.

    Runs inside exception handlers, so a coordinator failure is logged rather
    than replacing the caller's original error; the shield lets the refund
    finish even if the caller is cancelled again.
    """
    if reservation is None:
        return False
    cancel_budget = getattr(limiter, "cancel_rate_budget", None)
    if not callable(cancel_budget):
        return False
    try:
        return bool(await asyncio.shield(cancel_budget(reservation)))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("failed to cancel unsent rate reservation; estimate kept", exc_info=True)
        return False


def request_was_not_sent(started: bool, error: BaseException) -> bool:
    """True only when no request bytes can have reached the provider."""
    return not started or isinstance(error, UNSENT_TRANSPORT_ERRORS)


async def acquire(limiter, *, model: str, payload: dict, completion_tokens: int | None = None) -> None:
    """Wait for this model's rolling window; raises RateBudgetTimeout at the cap."""
    acquire_budget = getattr(limiter, "acquire_rate_budget", None)
    if not callable(acquire_budget):
        return
    rpm, tpm = rate_limits(model)
    await acquire_budget(
        provider=budget_provider(model),
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        reserved_tokens=min(tpm, estimate_request_tokens(payload, completion_tokens)),
        timeout_seconds=MODEL_RATE_BUDGET_WAIT_SECONDS,
    )
