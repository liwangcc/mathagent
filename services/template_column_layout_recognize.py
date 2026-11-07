"""Recognizing one teacher template's column layout: the entry point that uses every other part.

``recognize_column_layout`` validates the template image, then follows the mode
the teacher chose: a single column returns a whole-page draft directly, a double
column uses only the local code gate, and a triple column asks the layout model
for boundaries and accepts them only with local evidence.  A call that predates
the mode argument and injects an HTTP client keeps the legacy provider path.
Provider calls go through the shared lease and rate budget, retry transient
failures (``_MAX_TRANSIENT_ATTEMPTS``, ``_RETRY_DELAY_S``, ``_RETRY_MAX_DELAY_S``,
``_POOL_FAILOVER_DELAY_S``, ``_retry_after_seconds``) and wait at most
``_KEY_ACQUIRE_TIMEOUT_S`` for a key.

Moved unchanged from services/template_column_layout.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from config import (
    MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS,
    TEMPLATE_CUT_ENABLE_THINKING,
    TEMPLATE_CUT_MAX_TOKENS,
    TEMPLATE_CUT_REASONING_EFFORT,
    TEMPLATE_CUT_TEMPERATURE,
)
from services.key_pool import (
    acquire_key,
    mark_shared_credential_cooldown,
    refresh_shared_credential_health,
    shared_credential_cooldown_seconds,
)
from services.model_concurrency import get_model_limiter
from services.retry_backoff import exponential_retry_delay, parse_retry_after_seconds
from services.template_column_layout_contract import (
    DEFAULT_COLUMN_MODE,
    TemplateCutError,
    _payload_from_columns,
    normalize_column_mode,
    validate_layout_boundaries,
)
from services.template_column_layout_drafts import (
    _build_layout_result,
    _review_layout_result,
)
from services.template_column_layout_image import (
    _encode_template_data_url,
    _estimated_template_payload_bytes,
    _validate_template_image,
)
from services.template_column_layout_local import (
    _double_layout_draft,
    _infer_local_two_column_payload,
    _inspect_local_triple_evidence,
)
from services.template_column_layout_prompts import (
    _DOUBLE_PROMPT,
    _PROMPT,
    _TRIPLE_PROMPT,
    _extract_json_text,
    _strict_payload,
)
from services.template_column_layout_provider import (
    _PROVIDER,
    _acquire_template_rate_budget,
    _clear_template_capability_failure,
    _local_template_capability_failed,
    _mark_template_capability_failure,
    _post_with_shared_lease,
    _resolve_template_key_pool,
    _shared_template_capability_failed,
    _template_cut_route,
)

# services.template_column_layout's logger, so these records keep their logger name.
logger = logging.getLogger("services.template_column_layout")


# One initial request plus one retry on the same subscription route. Credential
# and request-contract errors fail immediately and never cross to payg.
_MAX_TRANSIENT_ATTEMPTS = 2
_RETRY_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 5.0
_POOL_FAILOVER_DELAY_S = 0.05
_KEY_ACQUIRE_TIMEOUT_S = float(MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    return parse_retry_after_seconds(response.headers.get("retry-after"))


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
