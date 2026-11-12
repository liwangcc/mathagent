"""Solver requests that outlast their page: the page is delivered, the answers follow.

Locating and transcribing a page takes a couple of minutes; solving one hard
question can take several times as long, and a page that waits for it keeps the
teacher waiting with nothing to review. When the caller accepts late answers
(``accepting``), a page is delivered once everything else is done and its solver
has had ``DELIVERY_WAIT_S``: questions whose solver request is still running are
marked ``answer_pending`` and the request keeps running, bounded by
``SOLVER_LIMIT_S`` instead of the page's deadline. ``LateAnswers`` then rebuilds
the page each time such a request ends, with the answer bound exactly as it
would have been on time, or with the question marked ``answer_failed``.

A caller that does not accept late answers waits for every answer, as before.
"""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from contextvars import ContextVar

from services import template_max_owned as max_owned
from services import template_solver_shards as solver_shards

# Seconds from the page's start that the page waits for its solver once the
# locator and the transcription are done. A healthy page solves in about two minutes.
DELIVERY_WAIT_S = 180.0
# Seconds from the page's start that a solver request may run when its answers may arrive late.
SOLVER_LIMIT_S = 900.0
PENDING_REASON = "标准答案仍在生成，完成后自动填入"
TIMED_OUT_REASON = "标准答案生成超时，请填写标准答案或重新导入模板"
FAILED_REASON = "标准答案生成失败，请填写标准答案或重新导入模板"
UNBOUND_REASON = "生成的标准答案无法与已交付的作答位对应，请填写标准答案或重新导入模板"


class Offer:
    """Where a page hands over the solver requests it did not wait for."""

    def __init__(self):
        self.late: LateAnswers | None = None


_offer: ContextVar[Offer | None] = ContextVar("template_late_answers", default=None)


@contextmanager
def accepting():
    """Recognize one page, taking answers that arrive after it was delivered."""
    offer = Offer()
    token = _offer.set(offer)
    try:
        yield offer
    finally:
        _offer.reset(token)


def offered() -> Offer | None:
    """The caller's offer to take late answers, if it made one."""
    return _offer.get()


def starter(offer: Offer | None, deadline: ContextVar, started: float):
    """``start(create, *args)`` for the page's solver tasks.

    With an offer the tasks are created in a context whose deadline is the
    solver's own limit, never earlier than the page's: a request that outlasts
    its page must not be stopped by the page's deadline.
    """
    current = deadline.get()
    if offer is None or current is None:
        return lambda create, *args, **kwargs: create(*args, **kwargs)
    context = contextvars.copy_context()
    context.run(deadline.set, (max(current[0], started + SOLVER_LIMIT_S), *current[1:]))
    return context.run


async def settle(required, shards, *, started: float, late: bool) -> list:
    """Results of ``required`` once the page can be put together.

    Every task is awaited and the first failure is raised at once, as
    ``asyncio.gather`` did. Only when late answers are accepted, solver shards
    still running after ``required`` finished and ``DELIVERY_WAIT_S`` passed
    since the page started are left running.
    """
    loop = asyncio.get_running_loop()
    waiting = {*required, *shards}
    while waiting:
        timeout = None
        if late and waiting.isdisjoint(required):
            timeout = started + DELIVERY_WAIT_S - loop.time()
            if timeout <= 0:
                break
        done, waiting = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if not done:
            break
    return [task.result() for task in required]


def _unanswered(owned, received: dict, reason: str) -> dict:
    """A shard result that answers nothing: every slot the locator found is left blank."""
    located = {"Q" + row["question_number"]: row for row in received["questions"]}
    return {"answers": [{"q_id": q_id, "standard_answer": "",
                         "answer_items": [{"slot_id": sid, "answer": ""} for sid in located[q_id]["slot_ids"]],
                         "slot_ids": list(located[q_id]["slot_ids"]), "solution_steps": [], "answer_variants": [],
                         "answer_review_reasons": [reason],
                         "solve_visual_context": {"facts": [], "relations": [], "uncertain": []}}
                        for q_id in owned],
            "directory_issues": [], "slot_source": max_owned.SHARED_SLOT_SOURCE, "max_flags": {}}


def _merged(results, manifest) -> dict:
    """One shard's result is the solver result; several are joined by question."""
    return results[0] if len(results) == 1 else solver_shards.merge_shard_results(results, manifest)


def solved(shards, plan, manifest, received: dict) -> dict:
    """The page's solver result; a shard that is still running answers nothing yet."""
    return _merged([task.result() if task.done() else _unanswered(owned, received, PENDING_REASON)
                    for task, owned in zip(shards, plan)], manifest)


class LateAnswers:
    """The solver requests of one delivered page that were still running."""

    def __init__(self, shards, plan, manifest, received: dict, rebuild):
        self._shards, self._plan, self._manifest = list(shards), [list(owned) for owned in plan], manifest
        self._received, self._rebuild = received, rebuild
        self._running = {index for index, task in enumerate(self._shards) if not task.done()}
        self._failed: dict[int, str] = {}

    def _numbers(self, indexes) -> list[str]:
        owned = {q_id for index in indexes for q_id in self._plan[index]}
        return [row["q_id"][1:] for row in self._manifest if row["q_id"] in owned]

    @property
    def pending(self) -> list[str]:
        """Page-local numbers of the questions still being solved, in reading order."""
        return self._numbers(self._running)

    def _solver(self) -> dict:
        return _merged([
            _unanswered(owned, self._received, self._failed.get(index, PENDING_REASON))
            if index in self._running or index in self._failed else task.result()
            for index, (task, owned) in enumerate(zip(self._shards, self._plan))], self._manifest)

    def marked(self, page: dict) -> dict:
        """``page`` with its unanswered questions named: pending ones and failed ones with the reason."""
        pending = set(self.pending)
        failed = {number: reason for index, reason in self._failed.items() for number in self._numbers([index])}
        for question in page["questions"]:
            number = question["question_number"]
            if number in pending:
                question["answer_pending"] = True
            if number in failed:
                question["answer_failed"] = failed[number]
        page["answers_pending"] = [number for number in self._numbers(range(len(self._plan))) if number in pending]
        page["answers_failed"] = failed
        return page

    async def updates(self):
        """Yield ``(page, arrived, failed)`` each time a request ends, until none is running.

        ``page`` is the whole page rebuilt with every answer known so far;
        ``arrived`` and ``failed`` are the page-local numbers that this step
        answered or gave up on. A request that fails, or whose answers cannot
        be bound to the page as delivered, fails only its own questions.
        """
        while self._running:
            done, _ = await asyncio.wait({self._shards[index] for index in self._running},
                                         return_when=asyncio.FIRST_COMPLETED)
            ended = sorted(index for index in self._running if self._shards[index] in done)
            self._running.difference_update(ended)
            for index in ended:
                task = self._shards[index]
                error = asyncio.CancelledError() if task.cancelled() else task.exception()
                if error is not None:
                    self._failed[index] = TIMED_OUT_REASON if isinstance(error, TimeoutError) else FAILED_REASON
            try:
                page = self._rebuild(self._solver())
            except Exception:
                # The page was put together without these answers; it still is.
                self._failed.update({index: UNBOUND_REASON for index in ended if index not in self._failed})
                page = self._rebuild(self._solver())
            arrived = self._numbers(index for index in ended if index not in self._failed)
            yield self.marked(page), arrived, self._numbers(index for index in ended if index in self._failed)

    def cancel(self) -> None:
        """Stop the requests that are still running; nobody is waiting for them."""
        for index in self._running:
            self._shards[index].cancel()


def deliver(offer: Offer | None, shards, plan, manifest, received: dict, page: dict, rebuild) -> dict:
    """``page`` as it leaves the pipeline: unfinished solver requests are handed to the offer."""
    if offer is None or all(task.done() for task in shards):
        return page
    offer.late = LateAnswers(shards, plan, manifest, received, rebuild)
    return offer.late.marked(page)
