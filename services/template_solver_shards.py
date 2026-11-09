"""Split the Max full-page solve into parallel question-group shards.

Every shard sends the same images and the same solver prompt, so the whole
directory stays visible as context and the shared prefix stays cacheable; only
a trailing ownership line differs, and each shard answers its own questions.
The plan is fixed from the Flash directory before any Max call: every question
has exactly one owner, so the merge never chooses between two answers.
"""
from __future__ import annotations

import asyncio
import copy
import re

from services import template_max_owned as max_owned

# A printed sub-question label continues the question before it: (1), （1）, ①.
_SUBPART_RE = re.compile(r"[(（]\s*[0-9]{1,2}\s*[)）]|[①-⑳]")


def question_groups(manifest) -> list[list[str]]:
    """Group consecutive q_ids that belong to one printed question.

    The Flash directory is flat, so a parent question is recognised from
    ``printed_path``: a leaf whose path has a sub-question label after at least
    one component joins the previous leaf when the path before that label is
    the previous leaf's key. A missed group only costs consistency (both
    shards still see the whole page), never an answer.
    """
    groups, previous = [], None
    for row in manifest:
        path = tuple(str(part).strip() for part in row.get("printed_path") or [])
        marker = next((i for i, part in enumerate(path) if _SUBPART_RE.fullmatch(part)), 0)
        key = path[:marker] if marker else path
        if marker and groups and key == previous:
            groups[-1].append(row["q_id"])
        else:
            groups.append([row["q_id"]])
        previous = key
    return groups


def plan_shards(manifest, shard_count: int) -> list[list[str]]:
    """Longest-first greedy split of question groups, weighted by position.

    Later questions are usually longer to solve, so a group weighs the sum of
    its reading-order positions; each group goes to the lightest shard. Shards
    keep reading order and are listed by their first question.
    """
    position = {row["q_id"]: index for index, row in enumerate(manifest, 1)}
    groups = question_groups(manifest)
    count = max(1, min(int(shard_count), len(groups)))
    shards, loads = [[] for _ in range(count)], [0] * count
    for group in sorted(groups, key=lambda g: (-sum(position[q] for q in g), position[g[0]])):
        target = loads.index(min(loads))
        shards[target].extend(group)
        loads[target] += sum(position[q] for q in group)
    return sorted((sorted(shard, key=position.__getitem__) for shard in shards),
                  key=lambda shard: position[shard[0]])


def configured_shard_count(hierarchy: dict) -> int:
    """Configured shards, bounded by the Max in-flight gate the locator shares."""
    import config
    if hierarchy.get("question_identity_version") != 4:
        return 1
    # The locator holds one Max permit; more shards than the rest would queue.
    return max(1, min(config.OCR_TEMPLATE_SOLVER_SHARDS,
                      config.OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT - 1))


def shard_prompt(prompt: str, owned) -> str:
    """Append the only shard-specific text after the shared prompt."""
    return (prompt + "\n本请求只负责以下题目：" + "、".join(owned)
            + "。目录中的其他题目由并行请求负责，可以阅读其题干和图形作为上下文，"
            "但answers只返回上述q_id，每个恰好一次，不要解答其他题目。")


def receive_shard(payload, manifest, owned) -> tuple[dict, int]:
    """Receive one shard strictly for its own questions.

    Answers for another shard's known questions are dropped and counted
    rather than re-bought; unknown, duplicate or missing owned questions still
    fail the shared receiver.
    """
    owned_ids = set(owned)
    others = {row["q_id"] for row in manifest} - owned_ids
    rows = payload.get("answers") if isinstance(payload, dict) else None
    dropped = 0
    if isinstance(rows, list):
        kept = [row for row in rows if not (isinstance(row, dict) and row.get("q_id") in others)]
        dropped = len(rows) - len(kept)
        payload = {**payload, "answers": kept}
    result = max_owned.receive_solver_candidates(
        payload, [row for row in manifest if row["q_id"] in owned_ids])
    return result, dropped


def merge_shard_results(results, manifest) -> dict:
    """Join shard answers by q_id into the single-request solver shape."""
    by_id, flags, issues = {}, {}, []
    for result in results:
        if result.get("slot_source") != max_owned.SHARED_SLOT_SOURCE:
            raise ValueError("解题分片的作答位合同版本不一致")
        for answer in result["answers"]:
            if answer["q_id"] in by_id:
                raise ValueError("解题分片返回了重复题目")
            by_id[answer["q_id"]] = answer
        for number, rows in (result.get("max_flags") or {}).items():
            flags.setdefault(number, []).extend(copy.deepcopy(rows))
        issues.extend(result["directory_issues"])
    order = [row["q_id"] for row in manifest]
    if set(by_id) != set(order):
        raise ValueError("解题分片没有覆盖全部题目")
    return {"answers": [by_id[qid] for qid in order], "directory_issues": issues,
            "slot_source": max_owned.SHARED_SLOT_SOURCE, "max_flags": flags}


async def solve_in_shards(request, images, prompt: str, manifest, plan, *,
                          phase: str, max_tokens: int, report: list) -> dict:
    """Run one ``request`` per shard concurrently and merge the answers.

    Images go before the prompt so every shard shares the same cacheable
    prefix. One failed shard cancels the others and fails the solve, exactly
    like a failed single request. ``report`` receives one entry per shard.
    """
    report.extend({"q_ids": list(owned), "out_of_scope_answers": 0} for owned in plan)

    def validator_for(index, owned):
        def validate(payload):
            result, dropped = receive_shard(payload, manifest, owned)
            report[index]["out_of_scope_answers"] = dropped
            return result
        return validate

    tasks = [asyncio.create_task(request(
        images, shard_prompt(prompt, owned), phase, validator_for(index, owned),
        max_tokens=max_tokens, reasoning_effort="high", expected_ids=tuple(owned),
        prompt_first=False, stage_key=f"{phase}#{index + 1}",
    ), name=f"template-qwen-max-fullsolve-{index + 1}") for index, owned in enumerate(plan)]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return merge_shard_results(results, manifest)
