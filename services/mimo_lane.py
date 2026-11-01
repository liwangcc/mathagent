"""Per-worker lane for teacher chat (Hermes) when it runs on the MiMo token plan.

Hermes takes this lane only when TEACHER_LLM_PROVIDER names that plan (the
code default); other providers keep just their key-pool and Redis leases.
The plan's hard concurrency is 8 (MIMO_SUBSCRIPTION_GLOBAL_CONCURRENCY). The
Redis lease enforces it across workers; this lane keeps one worker from
queueing more requests than the account can serve.
"""
from __future__ import annotations

import asyncio

from config import MIMO_SUBSCRIPTION_GLOBAL_CONCURRENCY

_lane: asyncio.Semaphore | None = None
_lane_loop: asyncio.AbstractEventLoop | None = None


def get_mimo_lane() -> asyncio.Semaphore:
    global _lane, _lane_loop
    loop = asyncio.get_running_loop()
    if _lane is None or _lane_loop is not loop:
        _lane = asyncio.Semaphore(MIMO_SUBSCRIPTION_GLOBAL_CONCURRENCY)
        _lane_loop = loop
    return _lane
