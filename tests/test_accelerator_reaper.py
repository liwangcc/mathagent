"""Lifecycle tests for the server-authoritative accelerator lease reaper."""
from __future__ import annotations

import asyncio

from services import accelerator_reaper


def test_reaper_loop_runs_once_immediately_then_stops(monkeypatch):
    calls: list[str] = []

    async def exercise() -> None:
        stop = asyncio.Event()

        def fake_reap() -> int:
            calls.append("reaped")
            stop.set()
            return 0

        monkeypatch.setattr(
            accelerator_reaper,
            "reap_expired_accelerator_jobs_once",
            fake_reap,
        )
        await accelerator_reaper.accelerator_reaper_loop(stop)

    asyncio.run(exercise())
    assert calls == ["reaped"]
