"""Merge step: page-local template question numbers -> paper-global numbers.

The hierarchy model numbers every page from ``"1"`` on its own
(``template_one_three.receive_hierarchy``), so each page can be recognized
without knowing what came before it.  The confirmed paper, however, keys
answers by one question number that is unique across all pages
(``TemplateSession.build_answer_map``, ``PaperCache.from_confirmed_template_pages``).

``globalize_page_numbers`` shifts one page result by the question count of all
earlier pages, deterministically and without another model call.  The model's
own number is kept as ``page_local_number``; ``printed_path`` is provenance and
is never rewritten.  Free-text review reasons (for example ``"3 作答框数量..."``)
are left untouched; only list entries that are exactly a local number move.
"""
from __future__ import annotations

import copy
import re

# Region-keyed lists of a page result.  Each item names its question by number
# and, for answer regions, by slot ids.
_REGION_LIST_KEYS = (
    "question_anchors",
    "template_answer_regions",
    "template_handwriting_regions",
    "question_anchor_candidates",
    "template_answer_region_candidates",
    "template_handwriting_region_candidates",
    "question_schema_candidates",
)
_SLOT_RE = re.compile(r"(.+)\[([1-9][0-9]*)\]")


def _entry_number(entry: dict) -> str:
    numbers = {
        str(entry[key]).strip()
        for key in ("question_no", "question_number")
        if entry.get(key) is not None and str(entry[key]).strip()
    }
    if len(numbers) != 1:
        raise ValueError("模板题目的 question_no 与 question_number 缺失或不一致")
    return numbers.pop()


def page_local_numbers(page_result: dict) -> list[str]:
    """Return the page's numbers, requiring the page-local ``1..n`` contract."""
    questions = page_result.get("questions") if isinstance(page_result, dict) else None
    if not isinstance(questions, list) or not questions:
        raise ValueError("模板页没有可合并的题目")
    numbers = [_entry_number(question) for question in questions if isinstance(question, dict)]
    if len(numbers) != len(questions):
        raise ValueError("模板题目必须为对象")
    if numbers != [str(index) for index in range(1, len(numbers) + 1)]:
        raise ValueError("模板页题号不是从 1 连续编号的页内编号，无法合并为整卷题号")
    return numbers


def _shift_slot(slot_id: object, local: str, number: str) -> str:
    match = _SLOT_RE.fullmatch(slot_id) if isinstance(slot_id, str) else None
    if match is None or match.group(1) != local:
        raise ValueError(f"作答位 {slot_id} 不属于第 {local} 题，无法合并为整卷题号")
    return f"{number}[{match.group(2)}]"


def _shift_slots_deep(value, local: str, number: str):
    """Rewrite every ``slot_id``/``slot_ids`` owned by one question."""
    if isinstance(value, list):
        return [_shift_slots_deep(item, local, number) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "slot_id" and item is not None:
            result[key] = _shift_slot(item, local, number)
        elif key == "slot_ids" and isinstance(item, list):
            result[key] = [_shift_slot(slot, local, number) for slot in item]
        else:
            result[key] = _shift_slots_deep(item, local, number)
    return result


def _rewrite_entry(entry: dict, numbers: dict[str, str], namespace, *, mark_local: bool) -> dict:
    """Rewrite one question, directory node or leaf in place and return it."""
    for key in ("node_id", "directory_node_id", "parent_id", "directory_parent_id"):
        if entry.get(key):
            entry[key] = namespace(entry[key])
    local = None
    if any(entry.get(key) is not None for key in ("question_no", "question_number")):
        local = _entry_number(entry)
        if local not in numbers:
            raise ValueError(f"题号 {local} 不在本页题目清单内")
        number = numbers[local]
        for key in ("question_no", "question_number"):
            if entry.get(key) is not None:
                entry[key] = number
        if entry.get("q_id"):
            if entry["q_id"] != "Q" + local:
                raise ValueError(f"题目 {local} 的 q_id 与题号不一致")
            entry["q_id"] = "Q" + number
        if entry.get("numeric_path") == local.split("."):
            entry["numeric_path"] = number.split(".")
        entry.update(_shift_slots_deep(
            {key: entry[key] for key in entry if key != "directory_metadata"}, local, number,
        ))
        if mark_local:
            entry["page_local_number"] = local
    if isinstance(entry.get("directory_metadata"), dict):
        _rewrite_entry(entry["directory_metadata"], numbers, namespace, mark_local=mark_local)
    return entry


def globalize_page_numbers(page_result: dict, offset: int, *, page_index: int) -> dict:
    """Return a copy of one page result renumbered into the paper's sequence.

    ``offset`` is the total question count of every earlier page in page order.
    Local question ``n`` becomes ``n + offset``, slot ``n[j]`` becomes
    ``(n + offset)[j]``, and directory node ``n001`` becomes ``p{page}-n001``.
    Anything that does not follow the page-local contract fails closed.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("题号偏移必须为非负整数")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise ValueError("page_index 必须为非负整数")
    if any(isinstance(question, dict) and "page_local_number" in question
           for question in (page_result or {}).get("questions") or []):
        raise ValueError("模板页题号已合并为整卷题号，不能重复偏移")
    local_numbers = page_local_numbers(page_result)
    numbers = {local: str(int(local) + offset) for local in local_numbers}
    prefix = f"p{page_index + 1}-"

    def namespace(node_id):
        return prefix + str(node_id)

    result = copy.deepcopy(page_result)
    result["questions"] = [
        _rewrite_entry(question, numbers, namespace, mark_local=True)
        for question in result["questions"]
    ]
    directory = result.get("question_directory")
    if isinstance(directory, dict):
        for key in ("nodes", "leaves"):
            if isinstance(directory.get(key), list):
                directory[key] = [
                    _rewrite_entry(node, numbers, namespace,
                                   mark_local=node.get("question_number") is not None)
                    if isinstance(node, dict) else node
                    for node in directory[key]
                ]
        if isinstance(directory.get("needs_review"), list):
            for issue in directory["needs_review"]:
                if isinstance(issue, dict) and issue.get("node_id"):
                    issue["node_id"] = namespace(issue["node_id"])
        directory["page_index"] = page_index
        directory["question_number_offset"] = offset
    for key in _REGION_LIST_KEYS:
        items = result.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                _rewrite_entry(item, numbers, namespace, mark_local=False)
    # Pending-review lists name questions by number; free-text reasons that do
    # not exactly equal a local number are kept verbatim.
    pending = result.get("requires_teacher_confirmation")
    if isinstance(pending, list):
        result["requires_teacher_confirmation"] = [
            numbers.get(str(value).strip(), value) for value in pending
        ]
    for key in ("anchor_diagnostics", "template_ocr_diagnostics"):
        diagnostics = result.get(key)
        if isinstance(diagnostics, dict) and isinstance(diagnostics.get("review_reasons"), list):
            diagnostics["review_reasons"] = [
                numbers.get(value.strip(), value) if isinstance(value, str) else value
                for value in diagnostics["review_reasons"]
            ]
    return result


def validate_global_numbering(pages: list[dict]) -> list[str]:
    """Fail closed unless the merged paper numbers its questions ``1..total``."""
    ordered = sorted(
        (page for page in pages or [] if isinstance(page, dict)),
        key=lambda page: int(page.get("page_index", 0)),
    )
    numbers = [
        str(question.get("question_no") or question.get("question_number") or "").strip()
        for page in ordered
        for question in page.get("questions") or []
        if isinstance(question, dict)
    ]
    if len(numbers) != len(set(numbers)):
        raise ValueError("整卷题号合并后仍有重复，请重新导入模板")
    if numbers != [str(index) for index in range(1, len(numbers) + 1)]:
        raise ValueError("整卷题号合并后没有从 1 连续编号，请重新导入模板")
    return numbers
