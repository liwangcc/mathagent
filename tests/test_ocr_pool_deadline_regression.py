import asyncio
from unittest.mock import patch

from services.ocr_pool_scheduler import LocalPoolCoordinator


def test_batch_deadline_start_never_resets_for_the_same_batch_key():
    async def run():
        coordinator = LocalPoolCoordinator(global_rpm=60, global_burst=1)
        with patch("services.ocr_pool_scheduler.time.time", return_value=1000.0):
            first = await coordinator.get_batch_start("batch-a", 300)
        with patch("services.ocr_pool_scheduler.time.time", return_value=1300.1):
            second = await coordinator.get_batch_start("batch-a", 300)
        return first, second

    assert asyncio.run(run()) == (1000.0, 1000.0)
