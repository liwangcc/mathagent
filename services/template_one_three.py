"""Strict receive boundary for hierarchy -> locator / image solver / text OCR.

No JSON repair, positional answer binding, or legacy solver fallback. The locator
owns types/geometry; the solver owns answers/facts; Flash owns non-choice text.
"""
from __future__ import annotations

import copy
import json
import re

from services import template_max_owned as max_owned

SOLVE_PHASE = "template-max-fullsolve-v1"
TRANSCRIBE_PHASE = "template-flash-transcribe-v1"
STRATEGY = "flash_hierarchy_one_three_v1"
MAX_TOKENS = 32768
FLASH_TOKENS = 8000
# Frozen AnswerSpec and visual evidence both accept at most 20 slots per question.
MAX_SLOTS_PER_QUESTION = max_owned.MAX_SLOTS_PER_QUESTION
# How a contract row names a page's first question when it carries no printed
# label: the rest of a question that began on the previous page. It is shown
# to the models only; the hierarchy keeps that question's printed label unknown.
CONTINUED_FROM_PREVIOUS_PAGE = max_owned.CONTINUED_FROM_PREVIOUS_PAGE


def _names_printed_number(label: object, path: list[str]) -> bool:
    """Whether ``label`` is a number printed in ``path``: ``17`` in ``17.`` or ``第17题``, not in ``117.``."""
    return (isinstance(label, str) and label.isascii() and label.isdigit()
            and any(re.search(rf"(?<![0-9]){label}(?![0-9])", part) for part in path))


def receive_hierarchy(payload: object, *, version: int | None = None) -> dict:
    """Receive shared slot inventory v4, or explicit historical identity v3.

    A question's identity on its page is its position: the returned ids are
    ``"1".."n"`` in row order. On real exam pages Flash v4 strays from that
    numbering in two ways, and both are the same rows under another name:

    - it writes a row's printed number (``"17"``, slots ``17[1]``) instead of
      the position. Accepted only when that row's own ``printed_path`` shows
      the number, so a gap in page numbering (``1, 2, 4``) is still rejected;
    - a page that opens with the rest of a question from the previous page
      has no printed label for it. The first row may therefore have an empty
      ``printed_path``; it stays empty (unknown), never a made-up label.
    """
    if (not isinstance(payload, dict) or set(payload) != {"contract_version", "questions"}
            or type(payload["contract_version"]) is not int or payload["contract_version"] not in {3, 4}
            or (version is not None and payload["contract_version"] != version)):
        raise ValueError("题目层级合同版本无效")
    shared = payload["contract_version"] == 4
    rows = payload["questions"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValueError("题目层级列表无效")
    nodes = []
    for index, row in enumerate(rows, 1):
        if (not isinstance(row, dict)
                or set(row) != ({"question_number", "printed_path", "slots"} if shared else {"question_number", "printed_path"})):
            raise ValueError("题目层级字段或连续编号无效")
        label, path = row["question_number"], row["printed_path"]
        if (not isinstance(path, list) or len(path) > 8
                or any(not isinstance(v, str) or len(v) > 128
                       or any(ord(ch) < 32 for ch in v) for v in path)):
            raise ValueError("题目层级标记无效")
        # Some primary-school papers have a section heading but no separate
        # printed number for the final word problem. Models may represent that
        # absent leaf label as one trailing empty component; canonicalize only
        # that unambiguous form and keep all other path components non-empty.
        if path and path[-1] == "":
            path = path[:-1]
        continued = shared and index == 1 and not path
        if (not path and not continued) or any(not value.strip() for value in path):
            raise ValueError("题目层级标记无效")
        printed = shared and (_names_printed_number(label, path)
                              or (continued and isinstance(label, str) and label.isascii() and label.isdigit()))
        if label != str(index) and not printed:
            raise ValueError("题目层级字段或连续编号无效")
        nodes.append({"id": str(index), "parent_id": None,
                      "raw_label": path[-1] if path else "", "source_path": list(path)})
        if shared:
            slots = row["slots"]
            if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS_PER_QUESTION:
                raise ValueError("Flash 作答位清单无效")
            for slot in slots:
                if (not isinstance(slot, dict) or set(slot) != {"slot_id", "cue"}
                        or not isinstance(slot["cue"], str) or not slot["cue"].strip()
                        or len(slot["cue"]) > 2000):
                    raise ValueError("Flash 作答位编号或说明无效")
            slot_ids = [f"{index}[{slot_index}]" for slot_index in range(1, len(slots) + 1)]
            # Slots are numbered under the same name as their question, whichever of the two it used.
            if [slot["slot_id"] for slot in slots] not in (
                    slot_ids, [f"{label}[{slot_index}]" for slot_index in range(1, len(slots) + 1)]):
                raise ValueError("Flash 作答位编号或说明无效")
            nodes[-1].update(slots=[{**slot, "slot_id": slot_id} for slot_id, slot in zip(slot_ids, slots)],
                             answer_slot_count=len(slots), slot_ids=slot_ids)
    return {"nodes": nodes, "leaf_ids": [n["id"] for n in nodes],
            "needs_review": [], "canonicalized": True,
            "question_identity_version": payload["contract_version"], "region_count_contract": shared,
            "typed_region_contract": False}


def coarse_follows_hierarchy(labels: list[str], hierarchy: dict) -> bool:
    """Whether the coarse locator's rows are the hierarchy's questions, in the same order.

    The two Flash requests number a page's questions independently. A coarse
    row numbered by its position is taken on trust, as it always was; a row
    that carries a printed number instead must name the number the hierarchy
    read at that position (anything goes for a first question whose label the
    page does not show).
    """
    nodes = {node["id"]: node for node in hierarchy["nodes"]}
    paths = [list(nodes[number].get("source_path") or []) for number in hierarchy["leaf_ids"]]
    return len(labels) == len(paths) and all(
        label == str(index) or _names_printed_number(label, path) or (index == 1 and not path)
        for index, (label, path) in enumerate(zip(labels, paths), 1))


def question_contract(hierarchy: dict) -> list[dict]:
    """Shared identity/inventory; geometry and types remain locator-owned."""
    nodes = {node["id"]: node for node in hierarchy["nodes"]}
    return [{"q_id": "Q" + number,
             **({"printed_path": list(nodes[number]["source_path"])} if nodes[number].get("source_path") else
                {"printed_path": [CONTINUED_FROM_PREVIOUS_PAGE], "note": max_owned.CONTINUED_NOTE}),
             **({"slots": copy.deepcopy(nodes[number]["slots"])}
                if hierarchy.get("question_identity_version") == 4 else {})}
            for number in hierarchy["leaf_ids"]]



def parse_json(text: str) -> dict:
    """Accept one complete JSON object (optionally fenced), never repair it."""
    if not isinstance(text, str):
        raise ValueError("模板响应必须是 JSON 文本")
    text = text.strip()
    if text.startswith("```"):
        fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text)
        if fence is None:
            raise ValueError("模板 JSON fence 不完整")
        text = fence.group(1)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("模板 JSON 字段重复")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("模板 JSON 包含非有限数字")

    result = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("模板响应必须是 JSON 对象")
    return result


def slot_contract(hierarchy: dict) -> list[dict]:
    if hierarchy.get("question_identity_version") == 4:
        return question_contract(hierarchy)
    nodes = {node["id"]: node for node in hierarchy["nodes"]}
    result = []
    for number in hierarchy["leaf_ids"]:
        node = nodes[number]
        kinds = node.get("area_kinds") or ["answer"] * node["answer_slot_count"]
        if not isinstance(kinds, list) or not kinds or any(k not in {"answer", "drawing"} for k in kinds):
            raise ValueError("模板槽位类型无效")
        identity = ({"printed_label": node["raw_label"], "cue": node["cue"]}
                    if hierarchy.get("question_identity_version") == 2 else {})
        result.append({"q_id": "Q" + number, **identity, "slots": [
            {"slot_id": f"{number}[{index}]", "answer_type": kind}
            for index, kind in enumerate(kinds, 1)
        ]})
    return result


def build_prompts(hierarchy: dict, *, education_stage: str, locator: dict | None = None) -> tuple[str, str]:
    labels = {"primary": "小学数学", "junior": "初中数学", "senior": "高中数学"}
    if education_stage not in labels:
        raise ValueError("模板必须提供学段")
    if hierarchy.get("question_identity_version") in {3, 4}:
        return max_owned.build_prompts(question_contract(hierarchy), education_stage=education_stage, locator=locator)
    contract = json.dumps(slot_contract(hierarchy), ensure_ascii=False, separators=(",", ":"))
    identity_hint = (
        "printed_label和cue仅用于原图认题，条件以原图印刷内容为准；q_id与slots已固定，不重编号、不拆并题。"
        if hierarchy.get("question_identity_version") == 2 else ""
    )
    choice_transcription = (
        "逐项返回目录中全部q_id；选择题保留该q_id，question_text为空字符串，不转录其题干或选项。"
        "先看题目是否要求从A/B/C/D备选结论中选择，有则整题正文留空；分析多种算法并填写理由的题不是选择题。"
        if hierarchy.get("question_identity_version") == 2 else "单选、多选整题省略；自行看图判断，题号按目录对应。"
    )
    solve = (
        identity_hint + f"按目录看完整原图独立解答{labels[education_stage]}印刷题，生成教师答案草稿。"
        "不等待或依赖其他模型转录，不抄完整题干或选项，只给标准答案、必要步骤、可见关键图形事实和uncertain。"
        "忽略学生手写答案、涂色、演算及教师批改，不能据此反推条件。"
        "只记录可见标值、刻度与明确关系；不凭外观猜测，不清楚处列入uncertain。"
        "选择题先独立求出结论，再逐字核对原图对应选项，标准答案与答案槽只给选项字母/完整字母集合；"
        "solution_steps写明结论及所选字母对应的原选项内容，不凭方向词或选项位置猜字母。开放题给合法答案范围和全部合法解。"
        "答案按slot_id逐槽返回；drawing槽answer严格为空字符串，作图要求写在standard_answer。"
        "同一槽求多个量不可拆槽，缺失答案留空并标uncertain；不得按答案内容重排。\n"
        f"目录：{contract}\n"
        '严格输出一个合法JSON对象，answers数组每个题目对象单独换行，不加代码围栏：{"answers":[{"q_id":"Q1","standard_answer":"答案",'
        '"answer_slots":[{"slot_id":"1[1]","answer":""}],'
        '"solution_steps":[],"visual_facts":[],"uncertain":[]}]}'
    )
    transcribe = (
        identity_hint + f"转录{labels[education_stage]}非选择题的印刷正文文字和算式，保留原式与印刷错误，空白写（）。"
        "忽略学生手写与教师批改，不解题，不补全条件。" + choice_transcription +
        "只录正文文字/算式，不录图示、图中标注、坐标刻度、图形关系或图中算法，不生成图示补丁。"
        "确实无法辨清的正文列入uncertain，不用图形描述替代题干。\n"
        f"目录：{contract}\n"
        '只输出JSON：{"questions":[{"q_id":"Q1","question_text":"正文文字与算式","uncertain":[]}]}'
    )
    return solve, transcribe


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} 必须为字符串数组")
    return list(value)


def _index(rows, key, expected, *, partial=False):
    if not isinstance(rows, list):
        raise ValueError("模板题号/槽位必须为数组")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str):
            raise ValueError("模板题号/槽位标识无效")
        identity = row[key]
        if identity not in expected or identity in result:
            raise ValueError("模板题号/槽位未知或重复")
        result[identity] = row
    if not partial and set(result) != set(expected):
        raise ValueError("模板题号/槽位缺失")
    return result


def receive_solver(payload: object, hierarchy: dict) -> dict:
    if hierarchy.get("question_identity_version") in {3, 4}:
        return max_owned.receive_solver(payload, question_contract(hierarchy))
    contract = slot_contract(hierarchy)
    if not isinstance(payload, dict) or set(payload) != {"answers"}:
        raise ValueError("独立解题必须返回 answers")
    rows = _index(payload["answers"], "q_id", [q["q_id"] for q in contract])
    answers = []
    for expected in contract:
        row = rows[expected["q_id"]]
        if set(row) != {"q_id", "standard_answer", "answer_slots", "solution_steps", "visual_facts", "uncertain"}:
            raise ValueError("独立解题字段无效，不接收完整题干或题型")
        if not isinstance(row["standard_answer"], str):
            raise ValueError("标准答案必须为文本")
        steps = _strings(row["solution_steps"], "solution_steps")
        facts = _strings(row["visual_facts"], "visual_facts")
        uncertain = _strings(row["uncertain"], "uncertain")
        ids = [slot["slot_id"] for slot in expected["slots"]]
        slots = _index(row["answer_slots"], "slot_id", ids)
        values, issues = [], list(uncertain)
        for identity in expected["slots"]:
            slot = slots[identity["slot_id"]]
            if set(slot) != {"slot_id", "answer"} or not isinstance(slot["answer"], str):
                raise ValueError("独立解题槽位字段无效")
            value = slot["answer"]
            if identity["answer_type"] == "drawing":
                if value != "":
                    raise ValueError("drawing 答案槽必须为空字符串")
            elif not value.strip():
                issues.append(f"作答区域 {identity['slot_id']} 缺少标准答案，需教师补充")
            values.append(value)
        if not row["standard_answer"].strip():
            issues.append("缺少标准答案，需教师补充")
        answers.append({
            "q_id": row["q_id"], "standard_answer": row["standard_answer"],
            "solution_steps": steps, "answer_variants": [], "answer_slots": values,
            "slot_ids": ids, "answer_slot_count": len(ids),
            "answer_status": "needs_review" if issues else "ready",
            "answer_review_reasons": issues,
            "solve_visual_context": {"facts": facts, "relations": [], "uncertain": uncertain},
        })
    return {"answers": answers, "directory_issues": []}


def receive_transcription(payload: object, hierarchy: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"questions"}:
        raise ValueError("正文转录必须返回 questions")
    rows = _index(payload["questions"], "q_id", [q["q_id"] for q in question_contract(hierarchy)],
                  partial=hierarchy.get("question_identity_version") not in {2, 3, 4})
    for row in rows.values():
        if set(row) != {"q_id", "question_text", "uncertain"} or not isinstance(row["question_text"], str):
            raise ValueError("正文转录字段无效，不接收图示附件")
        _strings(row["uncertain"], "uncertain")
        # Only the independent locator owns the question type. An empty row
        # can mean "choice skipped"; defer semantic emptiness to the merge.
    return copy.deepcopy(payload)


def adapt_transcription_payload(payload: object, hierarchy: dict) -> dict:
    """Restore only an unambiguous envelope; retain every identity/field gate.

    A complete bare question list or a singleton containing the full envelope
    carries the same data. Scalars, missing/duplicate IDs and extra fields are
    never guessed, repaired or filled in.
    """
    if isinstance(payload, list):
        if len(payload) == 1 and isinstance(payload[0], dict) and set(payload[0]) == {"questions"}:
            payload = payload[0]
        else:
            payload = {"questions": payload}
    return receive_transcription(payload, hierarchy)


def merge_branches(locator: dict, solver: dict, transcription: dict) -> dict:
    """Only the locator can authorize an omitted choice transcription."""
    result = (max_owned.bind_answers(locator, solver)
              if locator.get("slot_source") in max_owned.SLOT_SOURCES or solver.get("slot_source") in max_owned.SLOT_SOURCES
              else copy.deepcopy(solver))
    text_by_id = {row["q_id"]: row for row in transcription["questions"]}
    located = {"Q" + row["question_number"]: row for row in locator["questions"]}
    for answer in result["answers"]:
        geometry = located[answer["q_id"]]
        text = text_by_id.get(answer["q_id"])
        if geometry["question_type"] == "choice":
            if text is not None and text["question_text"].strip():
                answer["answer_review_reasons"].append("Flash 转录与 Max 选择题类型冲突，需教师核对")
            answer["question_text"] = ""
            if result.get("slot_source") in max_owned.SLOT_SOURCES:
                # The independent solver has already supplied the option value
                # as a keyed answer item. Keep prose in solution_steps rather
                # than persisting the explanatory summary as a choice value.
                answer["standard_answer"] = answer["answer_slots"][0]
            if geometry["choice_mode"] == "unknown":
                answer["answer_review_reasons"].append("选择题单选/多选类型待教师确认")
        else:
            if text is None:
                answer["question_text"] = ""
                answer["transcription_uncertain"] = ["Flash 省略了非选择题正文，需教师补充"]
            else:
                answer["question_text"] = text["question_text"]
                answer["transcription_uncertain"] = list(text["uncertain"])
                if not text["question_text"].strip() and not text["uncertain"]:
                    answer["transcription_uncertain"].append("Flash 非选择题正文为空，需教师补充")
            answer["answer_review_reasons"].extend(answer["transcription_uncertain"])
        if answer["answer_review_reasons"]:
            answer["answer_status"] = "needs_review"
    return result
