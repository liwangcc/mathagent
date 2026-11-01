"""Bounded async dispatch helpers for batch work.

The dispatcher bounds task *creation*, not merely the remote API call, so image
preprocessing and Base64 construction cannot fan out to an entire upload batch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def bounded_completion_map(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    max_concurrent: int,
) -> AsyncIterator[tuple[int, R]]:
    """Run bounded workers and yield every successful result as it completes.

    If one worker fails in the same completion window as other successful
    workers, the successful results are yielded first.  This lets callers
    durably checkpoint completed work before the failure cancels the rest.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")

    source = iter(enumerate(items))
    in_flight: dict[asyncio.Task[R], int] = {}

    def schedule_one() -> bool:
        try:
            index, item = next(source)
        except StopIteration:
            return False
        in_flight[asyncio.create_task(worker(item))] = index
        return True

    for _ in range(max_concurrent):
        if not schedule_one():
            break

    try:
        while in_flight:
            done, _ = await asyncio.wait(
                in_flight,
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed: list[tuple[int, R]] = []
            failures: list[BaseException] = []
            for task in done:
                index = in_flight.pop(task)
                try:
                    completed.append((index, task.result()))
                except BaseException as exc:
                    failures.append(exc)

            if not failures:
                for _ in completed:
                    schedule_one()

            for result in sorted(completed, key=lambda item: item[0]):
                yield result

            if failures:
                raise failures[0]
    finally:
        remaining = list(in_flight)
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)


async def teacher_waved_completion_map(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    teacher_id: str,
    batch_id: str,
    wave_size: int,
    max_concurrent: int,
) -> AsyncIterator[tuple[int, R]]:
    """Continuously refill the Flash batch window, yielding completed results.

    The name and wave arguments remain compatible with router callers. Teacher
    fairness belongs to the shared per-request Redis gate, not a whole-wave
    barrier or an active-teacher count. Legacy multi-pool settings cannot bring
    those retired scheduling caps back into the single-model route.
    """
    if wave_size < 1:
        raise ValueError("wave_size must be at least 1")
    async for result in bounded_completion_map(items, worker, max_concurrent=max_concurrent):
        yield result
