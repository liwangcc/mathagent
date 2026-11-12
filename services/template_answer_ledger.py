"""Which standard answers of a template are still being generated, and the ones that arrived late.

A page can be delivered before its slowest solver request ends
(``template_late_answers``). The import records those questions here, in
``late_answers.json`` beside the template's ``meta.json``, and their answers
when they arrive (``follow_page``). The ledger is a file of its own because
the import and every editing route rewrite ``meta.json`` whole: a background
write into it would be lost, or would undo theirs.

Readers lay the ledger over the saved pages (``overlay``): a question still
being solved is marked ``answer_pending``, an arrived answer fills the saved
question while that is still unanswered, a failed one is marked
``answer_failed``. An entry whose request can no longer be running (its
deadline passed, for instance after a restart) reads as failed, never as
pending: nothing waits for it forever.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from services import template_late_answers as late_answers
from services.grading_contract import AnswerSpec
from services.template_page_numbering import globalize_page_numbers
from services.template_session import _meta_io_lock

logger = logging.getLogger(__name__)

_FILE = "late_answers.json"
# Seconds a request may still need after its own limit to be bound and recorded.
_RECORD_GRACE_S = 30.0
# What a late answer brings; everything else of the question was delivered with the page.
_ANSWER_KEYS = ("standard_answer", "answer_slots", "slot_ids", "solution_steps", "answer_variants",
                "solve_visual_context", "max_flags")
_ORDINAL = re.compile(r"\[(\d+)\]$")
# The tasks following delivered pages, by template directory.
_following: dict[str, set[asyncio.Task]] = {}


def _path(template_dir) -> Path:
    return Path(template_dir) / _FILE


def _read(template_dir) -> dict:
    path = _path(template_dir)
    try:
        with _meta_io_lock(path), path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _mutate(template_dir, change) -> None:
    """Read, change and replace the ledger as one short filesystem transaction."""
    from services.quick_grader import BatchOperationBusy, batch_operation_lock
    for attempt in range(200):
        try:
            with batch_operation_lock("late-answers", str(template_dir)):
                entries = _read(template_dir)
                change(entries)
                path = _path(template_dir)
                temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                        json.dump({"version": 1, "entries": entries}, handle, ensure_ascii=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    with _meta_io_lock(path):
                        os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                return
        except BatchOperationBusy:
            if attempt == 199:
                raise
            time.sleep(0.01)


def begin(template_dir, node_ids, *, deadline_at: float) -> None:
    """Record that these questions (by directory node id) are being solved until ``deadline_at``."""
    def change(entries):
        entries.update({node_id: {"status": "pending", "deadline_at": float(deadline_at)} for node_id in node_ids})
    _mutate(template_dir, change)


def record(template_dir, arrived: dict, failed: dict) -> None:
    """Record answers that arrived (node id -> answer) and questions given up on (node id -> reason)."""
    def change(entries):
        entries.update({node_id: {"status": "ready", "answer": answer} for node_id, answer in arrived.items()})
        entries.update({node_id: {"status": "failed", "reason": reason} for node_id, reason in failed.items()})
    _mutate(template_dir, change)


def forget(template_dir) -> None:
    """Stop following this template's pages and drop its ledger: the template was confirmed or given up.

    Safe from any thread: a route that is not a coroutine runs outside the event loop.
    """
    for task in list(_following.pop(str(template_dir), ())):
        task.get_loop().call_soon_threadsafe(task.cancel)
    path = _path(template_dir)
    with _meta_io_lock(path):
        path.unlink(missing_ok=True)


def _state(entry, now: float) -> tuple[str, str]:
    """``(status, reason)`` of an entry as a reader sees it at ``now``."""
    entry = entry if isinstance(entry, dict) else {}
    if entry.get("status") == "pending":
        if now < float(entry.get("deadline_at") or 0):
            return "pending", ""
        return "failed", late_answers.TIMED_OUT_REASON
    if entry.get("status") == "ready" and isinstance(entry.get("answer"), dict):
        return "ready", ""
    return "failed", str(entry.get("reason") or late_answers.FAILED_REASON)


def _unanswered(question: dict) -> bool:
    return (not str(question.get("standard_answer") or "").strip()
            and not any(str(value or "").strip() for value in question.get("answer_slots") or []))


def _fill(question: dict, answer: dict) -> None:
    """Put a late answer into the saved question, slot by slot position within the question."""
    for key in ("standard_answer", "solution_steps", "answer_variants", "solve_visual_context", "max_flags"):
        if key in answer:
            question[key] = copy.deepcopy(answer[key])
        else:
            # A saved question carries these only when set; the blank it was delivered with set nothing to keep.
            question.pop(key, None)
    values = list(answer.get("answer_slots") or [])
    ordinals = [_ORDINAL.search(str(sid)) for sid in answer.get("slot_ids") or []]
    current = [_ORDINAL.search(str(sid)) for sid in question.get("slot_ids") or []]
    if current and len(ordinals) == len(values) and all(ordinals) and all(current):
        # The question may have been renumbered since: a slot keeps its place in the question.
        by_ordinal = {match.group(1): value for match, value in zip(ordinals, values)}
        question["answer_slots"] = [by_ordinal.get(match.group(1), "") for match in current]
    elif len(values) == len(question.get("answer_slots") or []):
        question["answer_slots"] = values


def overlay(template_dir, pages, *, now: float | None = None) -> list:
    """A copy of ``pages`` as the ledger completes them; saved pages without entries come back equal."""
    entries = _read(template_dir)
    result = copy.deepcopy(pages)
    if not entries:
        return result
    now = time.time() if now is None else now
    for page in result:
        for question in page.get("questions") or []:
            entry = entries.get(question.get("directory_node_id"))
            if entry is None or not _unanswered(question):
                continue
            status, reason = _state(entry, now)
            if status == "pending":
                question["answer_pending"] = True
            elif status == "ready":
                _fill(question, entry["answer"])
            else:
                question["answer_failed"] = reason
    return result


def pending(template_dir, pages, *, now: float | None = None) -> list[tuple[int, str]]:
    """``(page_index, question_no)`` of every saved question whose answer is still being generated."""
    return [(int(page.get("page_index") or 0), str(question.get("question_no")))
            for page in overlay(template_dir, pages, now=now)
            for question in page.get("questions") or [] if question.get("answer_pending")]


def _answer(question: dict) -> dict:
    """What a rebuilt question brings to the ledger, normalized like a saved question."""
    saved = AnswerSpec.model_validate(question).model_dump()
    return {key: saved[key] for key in _ANSWER_KEYS if key in saved}


async def _follow(template_dir, page_index: int, offset: int, node_ids: dict, late) -> None:
    """Record every update of one delivered page.

    If recording breaks, the questions still waiting are recorded as failed
    rather than left pending. A cancelled follow-up records nothing: whoever
    cancelled it owns the outcome (``forget``), and an interrupted process
    leaves entries that read as failed once their deadline passes.
    """
    waiting = dict(node_ids)
    try:
        async for rebuilt, arrived, failed in late.updates():
            reasons = dict(rebuilt["answers_failed"])
            page = globalize_page_numbers(rebuilt, offset, page_index=page_index)
            answers = {str(question["page_local_number"]): question for question in page["questions"]}
            ready, given_up = {}, {waiting.pop(number): reasons[number] for number in failed}
            for number in arrived:
                try:
                    ready[waiting.pop(number)] = _answer(answers[number])
                except ValueError:
                    # An answer the draft could not hold fails its own question, not the page's other ones.
                    given_up[node_ids[number]] = late_answers.UNBOUND_REASON
            await asyncio.to_thread(record, template_dir, ready, given_up)
    except asyncio.CancelledError:
        late.cancel()
        raise
    except Exception:
        logger.exception("迟到的标准答案记录失败 page=%s", page_index + 1)
        late.cancel()
        try:
            await asyncio.to_thread(
                record, template_dir, {}, dict.fromkeys(waiting.values(), late_answers.FAILED_REASON))
        except Exception:
            logger.exception("迟到的标准答案失败状态记录失败 page=%s", page_index + 1)


async def follow_page(template_dir, page_index: int, questions, late) -> None:
    """Start following a delivered page; ``questions`` are its saved (renumbered) questions.

    Its unanswered questions are recorded as pending before this returns, so no
    reader sees them as merely empty; each answer is recorded when it arrives.
    """
    local = {str(question["page_local_number"]): question for question in questions}
    node_ids = {number: local[number]["directory_node_id"] for number in late.pending}
    first = next(iter(local.values()))
    offset = int(first["question_no"]) - int(first["page_local_number"])
    await asyncio.to_thread(begin, template_dir, list(node_ids.values()),
                            deadline_at=time.time() + late_answers.SOLVER_LIMIT_S + _RECORD_GRACE_S)
    task = asyncio.create_task(_follow(template_dir, page_index, offset, node_ids, late),
                               name=f"template-late-answers-p{page_index + 1}")
    tasks = _following.setdefault(str(template_dir), set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
