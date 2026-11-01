"""Application-lifecycle lease reaper for opportunistic accelerator jobs."""
from __future__ import annotations

import asyncio
import logging
import os

from database import SessionLocal
from services.accelerator_jobs import expire_leases

logger = logging.getLogger(__name__)

REAPER_INTERVAL_SECONDS = int(os.environ.get("ACCELERATOR_REAPER_INTERVAL_SECONDS", "5"))
if not 1 <= REAPER_INTERVAL_SECONDS <= 60:
    raise RuntimeError("ACCELERATOR_REAPER_INTERVAL_SECONDS must be between 1 and 60")


def reap_expired_accelerator_jobs_once() -> int:
    """Reclaim expired leases using a fresh authoritative database session."""
    db = SessionLocal()
    try:
        return expire_leases(db)
    except Exception:
        db.rollback()
        logger.exception("Accelerator lease reaper failed")
        return 0
    finally:
        db.close()


async def accelerator_reaper_loop(stop_event: asyncio.Event) -> None:
    """Run until shutdown; startup recovery happens before the first wait."""
    while not stop_event.is_set():
        await asyncio.to_thread(reap_expired_accelerator_jobs_once)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REAPER_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue