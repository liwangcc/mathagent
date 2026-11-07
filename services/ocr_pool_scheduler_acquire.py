"""How the OCR pool scheduler gets pool capacity before it sends a call.

``_SchedulerAcquireMixin`` holds these ``OcrPoolScheduler`` methods (they use the
scheduler's pools, coordinator and settings through ``self``):

- ``_acquire_first`` gets the permit for a paper's first attempt before its
  deadline: the accelerator takes the papers ``_prefer_accelerator`` gives it
  (its configured share, by a stable hash), borrowed accelerator capacity is
  health-probed by one paper first, and otherwise the active primary pool is
  used, the standby pool only while the primary's circuit is open
  (``_pool_route_available`` is the route-availability hook);
- ``_acquire_retry`` and ``_wait_for_pool`` wait for capacity in one pool until a
  deadline; ``_sleep_before_retry`` backs off between tries;
  ``_remove_first_attempt_waiter`` takes a paper out of the coordinator's
  first-attempt queue;
- ``_acquire_template_fallback_rate_budget`` is the template fallback pool's
  admission hook before its permit (it sets no rate limit).

Moved unchanged from services/ocr_pool_scheduler.py; OcrPoolScheduler inherits them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from services.ocr_pool_scheduler_base import (
    OcrPoolError,
    PoolPermit,
    PoolSpec,
    _RETRY_BACKOFF_S,
)
from services.retry_backoff import jittered_backoff

# services.ocr_pool_scheduler's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_pool_scheduler")


class _SchedulerAcquireMixin:
    """OcrPoolScheduler's methods for getting pool capacity before a call."""

    async def _remove_first_attempt_waiter(
        self,
        teacher_hash: str,
        waiter_id: str,
    ) -> None:
        remove_waiter = getattr(self.coordinator, "remove_first_attempt_waiter", None)
        if remove_waiter is None:
            return
        try:
            await remove_waiter(teacher_hash, waiter_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The coordinator's short waiter TTL is a backstop. Cleanup must not
            # turn a completed/cancelled OCR dispatch into a user-facing failure.
            logger.warning("failed to remove OCR first-attempt waiter", exc_info=True)

    def _prefer_accelerator(self, route_key: str, paper_hash: str) -> bool:
        if self.accelerator_share <= 0:
            return False
        if self.accelerator_share >= 1:
            return True
        material = f"{route_key}\0{paper_hash}".encode("utf-8")
        bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        threshold = int(self.accelerator_share * (1 << 64))
        return bucket < threshold

    async def _pool_route_available(self, pool: PoolSpec) -> bool:
        del pool
        return True

    async def _acquire_template_fallback_rate_budget(self, payload: dict[str, Any]) -> None:
        """Admission seam before the in-flight permit; intentionally no rate gate.

        qwen3.8-max runs on a Token Plan subscription: Credits billing, no
        RPM/TPM quota. Its only provider limit is the tier concurrency, which
        the pool's hard_in_flight enforces (see config OCR_TEMPLATE_GPT_FALLBACK_*).
        """
        return None

    async def _sleep_before_retry(self, deadline_epoch: float) -> None:
        """Stagger one cross-pool retry without escaping its absolute budget."""
        remaining = deadline_epoch - time.time()
        if remaining <= 0:
            return
        delay = jittered_backoff(
            _RETRY_BACKOFF_S,
            jitter_ratio=3.0,
            max_jitter_s=0.25,
        )
        await asyncio.sleep(min(delay, remaining))

    async def _acquire_first(
        self,
        batch_hash: str,
        *,
        route_key: str,
        paper_hash: str,
        teacher_hash: str,
        deadline_epoch: float,
        waiter_id: str = "",
    ) -> tuple[PoolSpec, PoolPermit, bool]:
        route_availability: dict[str, bool] = {}

        async def route_available(pool: PoolSpec) -> bool:
            pool_key = pool.coordination_key
            available = route_availability.get(pool_key)
            if available is None:
                available = await self._pool_route_available(pool)
                route_availability[pool_key] = available
            return available

        accelerator_enabled = (
            self.accelerator_share > 0
            and await route_available(self.accelerator)
        )
        # A full accelerator share means the accelerator is the verified
        # primary lane, not an
        # opportunistic borrowed accelerator. Do not serialize a batch behind
        # one health probe before using its independent 30-stream window.
        accelerator_is_primary = self.accelerator_share >= 1.0
        prefer_accelerator = accelerator_enabled and self._prefer_accelerator(
            route_key, paper_hash
        )
        use_primary_hard_capacity = False
        probe_deferred = False
        while time.time() <= deadline_epoch:
            gate = (
                "healthy"
                if accelerator_enabled and accelerator_is_primary
                else await self.coordinator.accelerator_gate(batch_hash)
                if accelerator_enabled
                else "blocked"
            )
            accelerator_allowed = accelerator_enabled and gate == "healthy"
            probe_claimed = False
            # The first accelerator-selected attempt in a batch is a same-workload
            # health probe for borrowed accelerator capacity. Do not force
            # primary-selected papers onto the accelerator merely to probe it,
            # or small batches would exceed the
            # configured accelerator share substantially.
            if (
                accelerator_enabled
                and not accelerator_is_primary
                and prefer_accelerator
                and gate == "unknown"
                and not probe_deferred
            ):
                probe_claimed = await self.coordinator.claim_accelerator_probe(batch_hash)
                if probe_claimed:
                    try:
                        decision = await self.coordinator.try_acquire(
                            self.accelerator,
                            capacity_limit=self.accelerator.hard_in_flight,
                            borrowed=True,
                            use_global_gate=True,
                            teacher_hash=teacher_hash,
                            teacher_limit=0,
                            global_in_flight_limit=self.first_attempt_hard_in_flight,
                            global_teacher_limit=self.teacher_pool_in_flight,
                            waiter_id=waiter_id,
                        )
                    except asyncio.CancelledError:
                        try:
                            await self.coordinator.finish_accelerator_probe(
                                batch_hash, False
                            )
                        except Exception:
                            logger.exception(
                                "Failed to close cancelled accelerator probe dispatch gate"
                            )
                        raise
                    except Exception:
                        try:
                            await self.coordinator.abandon_accelerator_probe(
                                batch_hash
                            )
                        except Exception:
                            logger.exception(
                                "Failed to abandon errored accelerator probe dispatch gate"
                            )
                        raise
                    if decision.permit is not None:
                        if time.time() > deadline_epoch:
                            await decision.permit.release()
                            await self.coordinator.abandon_accelerator_probe(
                                batch_hash
                            )
                            break
                        self._stats["first_accelerator"] += 1
                        return self.accelerator, decision.permit, True
                    if decision.circuit_open:
                        await self.coordinator.finish_accelerator_probe(
                            batch_hash, False
                        )
                        gate = "blocked"
                    else:
                        # Capacity, priority, or pacing is temporarily unavailable.
                        # Release the claim and keep this paper moving through the
                        # primary route; a different paper may perform the
                        # same-workload probe.
                        await self.coordinator.abandon_accelerator_probe(batch_hash)
                        probe_deferred = True
                # Another request is already probing the accelerator. Keep the
                # batch moving through the primary pool; only future requests
                # may borrow accelerator capacity
                # after that one same-workload probe marks the gate healthy.
                accelerator_allowed = False

            choices: list[tuple[PoolSpec, int, bool, bool]] = []
            accelerator_choice = (
                self.accelerator,
                self.accelerator.hard_in_flight,
                True,
                probe_claimed,
            )
            active_name = await self.coordinator.get_active_primary(self.primary.name)
            first_pool = (
                self.failover_pool
                if self.failover_pool is not None and active_name == self.failover_pool.name
                else self.primary
            )
            standby_pool = (
                self.primary
                if self.failover_pool is not None and first_pool is self.failover_pool
                else self.failover_pool
            )
            primary_soft = (
                first_pool,
                (
                    first_pool.hard_in_flight
                    if use_primary_hard_capacity
                    else self.primary_soft_in_flight
                ),
                False,
                False,
            )
            if prefer_accelerator and accelerator_allowed:
                choices.extend((accelerator_choice, primary_soft))
            else:
                choices.append(primary_soft)
                if accelerator_allowed:
                    choices.append(accelerator_choice)
            if standby_pool is not None:
                choices.append((standby_pool, standby_pool.hard_in_flight, False, False))

            waits: list[float] = []
            all_circuits = True
            active_primary_circuit_open = not await route_available(first_pool)
            for pool, capacity, borrowed, is_probe in choices:
                if pool is standby_pool and not active_primary_circuit_open:
                    continue
                if not await route_available(pool):
                    continue
                decision = await self.coordinator.try_acquire(
                    pool,
                    capacity_limit=capacity,
                    borrowed=borrowed,
                    use_global_gate=True,
                    teacher_hash=teacher_hash,
                    teacher_limit=0,
                    global_in_flight_limit=self.first_attempt_hard_in_flight,
                    global_teacher_limit=self.teacher_pool_in_flight,
                    waiter_id=waiter_id,
                )
                if decision.permit is not None:
                    if time.time() > deadline_epoch:
                        await decision.permit.release()
                        break
                    key = "first_accelerator" if pool is self.accelerator else "first_primary"
                    self._stats[key] += 1
                    if pool is standby_pool:
                        await self.coordinator.set_active_primary(pool.name)
                    return pool, decision.permit, is_probe
                if pool is first_pool and decision.circuit_open:
                    active_primary_circuit_open = True
                all_circuits = all_circuits and decision.circuit_open
                waits.append(decision.wait_s)

            if choices and all_circuits:
                raise OcrPoolError(
                    "all_first_attempt_pools_circuit_open",
                    retryable=True,
                    transport_failure=True,
                )
            # Soft capacity means "try the accelerator first", not "leave paid primary
            # capacity idle".  A later pass may use the primary hard window.
            use_primary_hard_capacity = True
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.01, min(waits) if waits else 0.05), remaining, 0.25))
        raise OcrPoolError(
            "first_attempt_dispatch_deadline",
            # No provider request was sent. The caller still has the retry
            # window reserved above, so let the one-per-paper retry pool make
            # its attempt before this paper is left pending.
            retryable=True,
            transport_failure=False,
        )

    async def _acquire_retry(self, *, deadline_epoch: float) -> PoolPermit:
        return await self._wait_for_pool(
            self.retry_pool,
            capacity_limit=self.retry_pool.hard_in_flight,
            borrowed=False,
            use_global_gate=False,
            deadline_epoch=deadline_epoch,
        )

    async def _wait_for_pool(
        self,
        pool: PoolSpec,
        *,
        capacity_limit: int,
        borrowed: bool,
        use_global_gate: bool,
        deadline_epoch: float,
        teacher_hash: str = "",
        teacher_limit: int = 0,
    ) -> PoolPermit:
        while time.time() <= deadline_epoch:
            try:
                decision = await self.coordinator.try_acquire(
                    pool,
                    capacity_limit=capacity_limit,
                    borrowed=borrowed,
                    use_global_gate=use_global_gate,
                    teacher_hash=teacher_hash,
                    teacher_limit=teacher_limit,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis/coordinator failures happen before provider dispatch;
                # classify them explicitly so a provider route is not poisoned
                # by an infrastructure admission outage.
                raise OcrPoolError(
                    f"pool_admission_error:{pool.name}",
                    retryable=False,
                    transport_failure=False,
                    request_sent=False,
                    failure_category="admission",
                    actual_model=pool.model,
                ) from exc
            if decision.permit is not None:
                if time.time() > deadline_epoch:
                    await decision.permit.release()
                    break
                return decision.permit
            if decision.circuit_open:
                raise OcrPoolError(
                    f"pool_circuit_open:{pool.name}",
                    retryable=False,
                    # A circuit decision is local admission state.  It does
                    # not prove that a new provider request was attempted.
                    transport_failure=False,
                    request_sent=False,
                    failure_category="admission",
                    actual_model=pool.model,
                )
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.01, decision.wait_s), remaining, 0.25))
        raise OcrPoolError(
            f"pool_dispatch_deadline:{pool.name}",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="admission",
            actual_model=pool.model,
        )
