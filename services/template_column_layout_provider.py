"""The provider route of column layout: qwen3.8-flash on dedicated pay-as-you-go DashScope keys.

- ``_template_cut_route`` resolves the route: the settings page's template group
  when it is set, otherwise the dedicated ``TEMPLATE_CUT_*`` configuration, with
  no fallback to other credentials.  ``_resolve_template_key_pool`` and
  ``_cached_template_key_pool`` keep one ``KeyPool`` for it (``_template_pool``,
  rebuilt when the keys change).
- A capability failure cools the provider down in this process and, through the
  limiter, for every worker (``_mark_template_capability_failure``,
  ``_clear_template_capability_failure``, ``_local_template_capability_failed``,
  ``_shared_template_capability_failed``).
- ``get_column_layout_readiness`` reports readiness without a billable request.
- ``_post_with_shared_lease`` sends one request under the shared concurrency
  lease; ``_acquire_template_rate_budget`` waits for the model's RPM/TPM window.

The module-level state (``_template_pool``, ``_template_pool_signature``,
``_template_capability_unavailable_until``) lives here, next to the functions
that assign it.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import hashlib
import logging
import time
from types import SimpleNamespace
from typing import Any

import httpx

from config import (
    DASHSCOPE_GLOBAL_CONCURRENCY,
    TEMPLATE_CUT_API_KEY,
    TEMPLATE_CUT_API_KEYS,
    TEMPLATE_CUT_API_KEY_WEIGHTS,
    TEMPLATE_CUT_BASE_URL,
    TEMPLATE_CUT_KEY_CONCURRENCY,
    TEMPLATE_CUT_MAX_TOKENS,
    TEMPLATE_CUT_MODEL,
    TEMPLATE_CUT_TEMPERATURE,
    TEMPLATE_CUT_TIMEOUT_S,
)
from services import dashscope_budget
from services.key_pool import KeyPool, refresh_shared_credential_health
from services.model_concurrency import (
    RateBudgetTimeout,
    configured_fair_share_policy,
    get_model_limiter,
)
from services.template_column_layout_contract import TemplateCutError

# services.template_column_layout's logger, so these records keep their logger name.
logger = logging.getLogger("services.template_column_layout")


# qwen3.8-flash 与批改/转录共享 DashScope 按量账号，限流与凭证健康按
# provider="dashscope" 同域协调。
_PROVIDER = "dashscope"
_TEMPLATE_CAPABILITY = f"{_PROVIDER}:template_vision"


_CAPABILITY_FAILURE_COOLDOWN_S = 60.0


_template_pool: KeyPool | None = None
_template_pool_signature = ""
_template_capability_unavailable_until = 0.0


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
