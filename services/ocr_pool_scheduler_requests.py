"""How the OCR pool scheduler runs one OCR request end to end.

``_SchedulerRequestsMixin`` holds these ``OcrPoolScheduler`` methods:

- ``request`` runs one paper of a batch: it sizes the batch deadline
  (``_batch_deadline_for_papers``), knows teacher, batch and paper only by
  hashes (``_teacher_hash``, ``_batch_hash``), sends the first attempt and, if
  that fails retryably, makes the one cross-pool retry the batch budget allows
  (``_run_retry``, ``_RETRY_LEDGER_TTL_S``); an interactive request waits for the
  accelerator first (``_interactive_request``);
- ``request_template_fallback_pool`` runs one template request directly on the
  qwen3.8-max Token Plan pool and records the route it used in the caller's
  diagnostics (``_set_template_route_diagnostics``); a request whose stream
  died before an answer (``_stream_died_before_an_answer``) is sent once more.

Moved unchanged from services/ocr_pool_scheduler.py; OcrPoolScheduler inherits the methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
import uuid
from typing import Any

from services.ocr_pool_scheduler_base import (
    OcrPoolError,
    ResponseValidator,
    TEMPLATE_QWEN_FALLBACK_ROUTE,
)

# services.ocr_pool_scheduler's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_pool_scheduler")


_RETRY_LEDGER_TTL_S = 86_400.0


def _set_template_route_diagnostics(
    diagnostics: dict[str, object] | None,
    *,
    route: str,
    reasoning_effort: str,
) -> None:
    """Record the provider lane actually used by a template request.

    A mutable, caller-owned diagnostics object keeps the route visible without
    changing the string response contract or leaking provider credentials.
    """
    if not isinstance(diagnostics, dict):
        return
    diagnostics["actual_route"] = str(route)
    diagnostics["actual_reasoning_effort"] = str(reasoning_effort)


def _stream_died_before_an_answer(error: OcrPoolError) -> bool:
    """Tell whether a sent request failed in transport without the provider's verdict.

    That is a response that never came, or a stream that ended, stalled or
    broke before a complete answer. Everything else is left alone: an HTTP
    status (throttling, authentication, a server error) is the provider's own
    verdict and drives the pool circuit; an attempt that used its whole time
    budget leaves nothing to resend in; an answer the provider cut at the
    token limit, refused, or that the validator rejected is not a dead stream.
    """
    return (
        error.transport_failure
        and error.retryable
        and error.status_code is None
        and error.request_sent is not False
        and error.code != "absolute_attempt_ttl"
    )


class _SchedulerRequestsMixin:
    """OcrPoolScheduler's methods for running one OCR request end to end."""

    def _batch_deadline_for_papers(self, paper_count: int | None) -> float:
        """Use the actual batch size without weakening one complete retry."""
        if paper_count is None or self.batch_target_papers is None:
            return self.batch_deadline_s
        if isinstance(paper_count, bool) or not isinstance(paper_count, int):
            raise ValueError("batch paper count must be a positive integer")
        if paper_count < 1:
            raise ValueError("batch paper count must be a positive integer")
        assert self.batch_target_deadline_s is not None
        target_windows = math.ceil(paper_count / self.batch_target_papers)
        target_deadline = target_windows * self.batch_target_deadline_s
        complete_retry_reserve = (
            max(
                self.primary.attempt_ttl_s,
                self.failover_pool.attempt_ttl_s if self.failover_pool else 0,
                self.accelerator.attempt_ttl_s,
            )
            + self.retry_pool.attempt_ttl_s
            + self.final_guard_s
        )
        return max(
            complete_retry_reserve,
            min(target_deadline, self.batch_deadline_s),
        )

    def _teacher_hash(self, teacher_id: str) -> str:
        if not str(teacher_id or "").strip():
            raise OcrPoolError(
                "missing_teacher_identity", retryable=False, transport_failure=False
            )
        return hmac.new(
            self._teacher_hash_secret,
            str(teacher_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]

    def _batch_hash(self, batch_key: str) -> str:
        return hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:24]

    async def request(
        self,
        payload: dict[str, Any],
        *,
        batch_key: str,
        paper_key: str,
        teacher_id: str,
        validator: ResponseValidator | None = None,
        priority: str = "batch",
        retry_batch_key: str | None = None,
        retry_paper_key: str | None = None,
        batch_paper_count: int | None = None,
        batch_deadline_s: float | None = None,
    ) -> str:
        if getattr(self, "_template_route", False):
            # Template calls must enter request_template_fallback_pool().
            # Keeping the generic batch graph closed prevents an old caller
            # from silently reactivating a retired pool route.
            if not getattr(self, "_template_primary_available", False):
                raise OcrPoolError(
                    "template_primary_pool_unconfigured",
                    retryable=False,
                    transport_failure=False,
                )
        teacher_hash = self._teacher_hash(teacher_id)
        batch_hash = self._batch_hash(batch_key)
        paper_hash = hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:24]
        computed_batch_deadline_s = self._batch_deadline_for_papers(
            batch_paper_count
        )
        if batch_deadline_s is None:
            batch_deadline_s = computed_batch_deadline_s
        else:
            try:
                batch_deadline_s = float(batch_deadline_s)
            except (TypeError, ValueError) as exc:
                raise ValueError("batch_deadline_s must be finite and positive") from exc
            if not math.isfinite(batch_deadline_s) or batch_deadline_s <= 0:
                raise ValueError("batch_deadline_s must be finite and positive")
            batch_deadline_s = max(
                computed_batch_deadline_s,
                batch_deadline_s,
            )
        batch_started = await self.coordinator.get_batch_start(
            batch_hash, batch_deadline_s
        )
        phase_hash = self._batch_hash(f"{batch_hash}:{batch_started:.6f}")
        retry_batch_hash = (
            self._batch_hash(retry_batch_key)
            if retry_batch_key is not None
            else phase_hash
        )
        retry_paper_hash = (
            hashlib.sha256(retry_paper_key.encode("utf-8")).hexdigest()[:24]
            if retry_paper_key is not None
            else paper_hash
        )
        if priority == "interactive" and self.accelerator_share > 0:
            return await self._interactive_request(
                payload,
                retry_batch_hash,
                retry_paper_hash,
                teacher_hash,
                batch_started,
                validator,
                batch_deadline_s,
            )

        # A healthy first attempt must not be held behind capacity for every
        # hypothetical retry in the batch.  Each actual retry is separately
        # rate-limited and deadline-bound in _run_retry.  Reserving the whole
        # batch's retry token span here can place this deadline before the
        # batch even begins when the retry pool has a lower RPM.
        latest_first_start = (
            batch_started
            + batch_deadline_s
            - max(
                self.primary.attempt_ttl_s,
                self.failover_pool.attempt_ttl_s if self.failover_pool else 0,
                self.accelerator.attempt_ttl_s,
            )
        )
        first_waiter_id = uuid.uuid4().hex
        try:
            try:
                pool, permit, accelerator_probe = await self._acquire_first(
                    phase_hash,
                    route_key=paper_key,
                    paper_hash=paper_hash,
                    teacher_hash=teacher_hash,
                    waiter_id=first_waiter_id,
                    deadline_epoch=latest_first_start,
                )
            except BaseException:
                await self._remove_first_attempt_waiter(
                    teacher_hash,
                    first_waiter_id,
                )
                raise
        except OcrPoolError as first_error:
            if not first_error.retryable:
                raise
            return await self._run_retry(
                payload,
                validator=validator,
                retry_batch_hash=retry_batch_hash,
                retry_paper_hash=retry_paper_hash,
                batch_started=batch_started,
                batch_deadline_s=batch_deadline_s,
                first_error=first_error,
            )
        try:
            content = await self._execute(pool, payload, validator)
        except asyncio.CancelledError:
            if accelerator_probe:
                try:
                    await self.coordinator.finish_accelerator_probe(
                        phase_hash, False
                    )
                except Exception:
                    logger.exception(
                        "Failed to close cancelled accelerator health probe gate"
                    )
            raise
        except OcrPoolError as first_error:
            if accelerator_probe:
                await self.coordinator.finish_accelerator_probe(phase_hash, False)
            if not first_error.retryable:
                raise
            # Do not hold a failed first-attempt slot while waiting for the
            # configured retry lane. Upstream work may linger, but our bounded lease has
            # already ended and keeping the local/Redis slot would deadlock bursts.
            await permit.release()
            if (
                first_error.transport_failure
                and pool is self.primary
                and self.failover_pool is not None
                and await self.coordinator.is_circuit_open(
                    self.primary.coordination_key
                )
            ):
                await self.coordinator.set_active_primary(self.failover_pool.name)
            return await self._run_retry(
                payload,
                validator=validator,
                retry_batch_hash=retry_batch_hash,
                retry_paper_hash=retry_paper_hash,
                batch_started=batch_started,
                batch_deadline_s=batch_deadline_s,
                first_error=first_error,
            )
        else:
            if accelerator_probe:
                await self.coordinator.finish_accelerator_probe(phase_hash, True)
            return content
        finally:
            await permit.release()

    async def _run_retry(
        self,
        payload: dict[str, Any],
        *,
        validator: ResponseValidator | None,
        retry_batch_hash: str,
        retry_paper_hash: str,
        batch_started: float,
        batch_deadline_s: float,
        first_error: OcrPoolError,
    ) -> str:
        latest_retry_start = (
            batch_started
            + batch_deadline_s
            - self.retry_pool.attempt_ttl_s
            - self.final_guard_s
        )
        if time.time() > latest_retry_start:
            self._stats["retry_rejected"] += 1
            raise OcrPoolError(
                "retry_budget_exhausted",
                retryable=False,
                transport_failure=False,
            ) from first_error
        await self._sleep_before_retry(latest_retry_start)
        if not await self.coordinator.claim_retry(
            retry_batch_hash,
            retry_paper_hash,
            self.max_batch_retries,
            max(_RETRY_LEDGER_TTL_S, batch_deadline_s),
        ):
            self._stats["retry_rejected"] += 1
            raise OcrPoolError(
                "batch_retry_limit_reached",
                retryable=False,
                transport_failure=False,
            ) from first_error
        try:
            retry_permit = await self._acquire_retry(
                deadline_epoch=latest_retry_start
            )
        except BaseException:
            try:
                await self.coordinator.release_retry_claim(
                    retry_batch_hash,
                    retry_paper_hash,
                )
            except Exception:
                logger.exception("Failed to release unused OCR retry reservation")
            raise
        try:
            self._stats["retry_started"] += 1
            content = await self._execute(self.retry_pool, payload, validator)
            self._stats["retry_succeeded"] += 1
            return content
        finally:
            await retry_permit.release()

    async def request_template_fallback_pool(
        self,
        payload: dict[str, Any],
        *,
        request_key: str,
        validator: ResponseValidator | None = None,
        route_diagnostics: dict[str, object] | None = None,
        reasoning_effort: str = "high",
    ) -> str:
        """Run one template request on the qwen3.8-max Token Plan pool.

        This is intentionally a direct pool request: only the Token Plan
        in-flight gate (and an optional rate budget) admits it. A request
        whose stream died before an answer is sent once more inside the same
        in-flight slot; the caller's own deadline still bounds both sends.
        """
        from config import REASONING_EFFORTS

        if not request_key:
            raise ValueError("request_key is required")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("template Qwen reasoning must be one of " + ", ".join(REASONING_EFFORTS))
        fallback_pool = self.template_fallback_pool
        if fallback_pool is None:
            raise OcrPoolError(
                "template_fallback_pool_unconfigured",
                retryable=False,
                transport_failure=False,
            )
        if isinstance(route_diagnostics, dict):
            route_diagnostics["fallback_attempted"] = True
            route_diagnostics["replacement_attempted"] = True
            route_diagnostics["actual_model"] = str(fallback_pool.model)
            # Route selection is visible immediately for timeout budgeting,
            # but the paid-request one-shot marker is set only after TPM and
            # in-flight admission succeed.  This lets a rejected admission
            # retry without making diagnostics claim that Qwen was contacted.
            route_diagnostics["_fallback_admission_pending"] = True
        _set_template_route_diagnostics(
            route_diagnostics,
            route=TEMPLATE_QWEN_FALLBACK_ROUTE,
            reasoning_effort=reasoning_effort,
        )
        permit = None
        try:
            await self._acquire_template_fallback_rate_budget(payload)
            fallback_payload = dict(payload)
            # Emergency callers retain high; the dedicated transcription leg
            # explicitly requests low instead of inheriting payload metadata.
            fallback_payload["model_reasoning_effort"] = reasoning_effort
            deadline = time.time() + fallback_pool.attempt_ttl_s
            permit = await self._wait_for_pool(
                fallback_pool,
                capacity_limit=fallback_pool.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
                deadline_epoch=deadline,
            )
            # This marker means the Qwen request crossed both TPM admission and
            # the in-flight permit boundary and is about to enter provider
            # transport. Keep it unset while either admission step is pending,
            # so diagnostics cannot claim a request was sent when it was only
            # queued or rejected locally.
            if isinstance(route_diagnostics, dict):
                route_diagnostics["_fallback_admission_pending"] = False
                route_diagnostics["_fallback_request_started"] = True
            try:
                return await self._execute(fallback_pool, fallback_payload, validator)
            except OcrPoolError as first_error:
                if not _stream_died_before_an_answer(first_error):
                    raise
                # The provider can cut a stream mid-reasoning (HTTP 200, no
                # finish_reason, no [DONE]). Without this the whole template
                # page fails on one lost stream. One resend only: a second
                # failure is the request's failure.
                logger.warning(
                    "Template request is sent once more after %s pool=%s",
                    first_error.code,
                    fallback_pool.name,
                )
                if isinstance(route_diagnostics, dict):
                    route_diagnostics["resent_after"] = first_error.code
                return await self._execute(fallback_pool, fallback_payload, validator)
        finally:
            if isinstance(route_diagnostics, dict) and not route_diagnostics.get(
                "_fallback_request_started"
            ):
                route_diagnostics["_fallback_admission_pending"] = False
            if permit is not None:
                await permit.release()

    async def _interactive_request(
        self,
        payload: dict[str, Any],
        retry_batch_hash: str,
        retry_paper_hash: str,
        teacher_hash: str,
        batch_started: float,
        validator: ResponseValidator | None,
        batch_deadline_s: float,
    ) -> str:
        waiter_id = uuid.uuid4().hex
        await self.coordinator.add_priority_waiter(
            self.accelerator.coordination_key,
            waiter_id,
            self.accelerator.attempt_ttl_s + 30.0,
        )
        deadline = time.time() + self.accelerator.attempt_ttl_s
        try:
            permit = await self._wait_for_pool(
                self.accelerator,
                capacity_limit=self.accelerator.hard_in_flight,
                borrowed=False,
                use_global_gate=False,
                teacher_hash=teacher_hash,
                teacher_limit=self.teacher_pool_in_flight,
                deadline_epoch=deadline,
            )
            try:
                return await self._execute(self.accelerator, payload, validator)
            except OcrPoolError as first_error:
                if not first_error.retryable:
                    raise
                await permit.release()
                return await self._run_retry(
                    payload,
                    validator=validator,
                    retry_batch_hash=retry_batch_hash,
                    retry_paper_hash=retry_paper_hash,
                    batch_started=batch_started,
                    batch_deadline_s=batch_deadline_s,
                    first_error=first_error,
                )
            finally:
                await permit.release()
        finally:
            await self.coordinator.remove_priority_waiter(
                self.accelerator.coordination_key, waiter_id
            )
