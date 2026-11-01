"""Physical answer identity. Never infer ownership from response position."""
from __future__ import annotations

import copy
import json
import re


def normalize_slot_ids(question_number: str, slot_ids: object, count: int) -> list[str]:
    if not isinstance(question_number, str) or not question_number:
        raise ValueError("slot parent must be a question number")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("invalid slot count")
    if not isinstance(slot_ids, list) or len(slot_ids) != count:
        raise ValueError("slot_ids must align with physical regions")
    pattern = re.compile(re.escape(question_number) + r"\[[1-9][0-9]*\]")
    if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in slot_ids):
        raise ValueError("slot_ids must have the exact parent and positive index")
    if len(set(slot_ids)) != count:
        raise ValueError("duplicate slot_ids")
    return list(slot_ids)


def make_slot_ids(question_number: str, count: int) -> list[str]:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("invalid slot count")
    return normalize_slot_ids(question_number, [f"{question_number}[{i}]" for i in range(1, count + 1)], count)


def slot_label_lines(question_number: str, slot_id: str) -> list[str]:
    """Display hierarchy only; frozen bracket IDs remain the wire identity."""
    normalize_slot_ids(question_number, [slot_id], 1)
    parts = question_number.split(".")
    return [parts[0], *("." + part for part in parts[1:]),
            "." + slot_id[len(question_number) + 1:-1]]


def text_slot_ids(question_number: str, contract: dict) -> list[str]:
    ids = normalize_slot_ids(question_number, contract["slot_ids"], len(contract["slot_ids"]))
    kinds = contract.get("region_kinds") or ["answer"] * len(ids)
    if len(kinds) != len(ids) or any(kind not in {"answer", "drawing"} for kind in kinds):
        raise ValueError("region_kinds must align with slot_ids")
    return [slot for slot, kind in zip(ids, kinds) if kind == "answer"]


def normalize_student_slots(raw: object, expected: list[str]) -> list[dict]:
    """Validate exact ID coverage and states, then order by the frozen contract."""
    if not isinstance(raw, list):
        raise ValueError("student_slots missing for contracted question")
    by_id = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("student_slots must contain objects")
        slot_id = item.get("slot_id")
        if not isinstance(slot_id, str) or slot_id not in expected or slot_id in by_id:
            raise ValueError("unknown or duplicate student slot_id")
        state, answer = item.get("answer_state"), item.get("student_answer")
        if state not in {"visible", "blank", "unreadable"} or not isinstance(answer, str):
            raise ValueError("invalid student slot state or text")
        answer = answer.strip()
        if (state == "blank" and answer) or (state == "visible" and (not answer or answer == "[无法识别]")):
            raise ValueError("student slot state contradicts text")
        by_id[slot_id] = {"slot_id": slot_id, "student_answer": "[无法识别]" if state == "unreadable" else answer, "answer_state": state}
    if set(by_id) != set(expected):
        raise ValueError("missing student slot_id")
    return [by_id[slot] for slot in expected]


def bind_student_slots(question: dict, question_number: str, contract: dict) -> dict:
    """An invalid contracted response becomes unreadable, never positional text."""
    result = copy.deepcopy(question)
    expected = text_slot_ids(question_number, contract)
    if not expected:
        # Pure drawing is judged by its pixel evidence, not invented text slots.
        raw_slots = question.get("student_slots")
        if raw_slots not in (None, []):
            raise ValueError("pure drawing cannot carry textual student_slots")
        return result
    try:
        slots = normalize_student_slots(question.get("student_slots"), expected)
    except ValueError as exc:
        slots = [{"slot_id": slot, "student_answer": "[无法识别]", "answer_state": "unreadable"} for slot in expected]
        result["slot_identity_error"] = str(exc)
    result["student_slots"] = slots
    result["student_answer"] = "；".join(item["student_answer"] for item in slots)
    result["answer_state"] = (
        "unreadable" if result.get("slot_identity_error") or any(item["answer_state"] == "unreadable" for item in slots)
        else "visible" if any(item["answer_state"] == "visible" for item in slots) else "blank"
    )
    return result


def ocr_failed_placeholder(question_number: str, question_type: object, blank_count: object = 1) -> dict:
    """Fail-closed review row for a page whose OCR produced no usable evidence."""
    question_type = str(question_type or "solve").strip().lower()
    unreadable = "[无法识别]"
    if question_type == "fill":
        try:
            count = max(1, int(blank_count or 1))
        except (TypeError, ValueError):
            count = 1
        if count > 1:
            unreadable = "；".join("[无法识别]" for _ in range(count))
    return {
        "question_no": question_number,
        "student_answer": unreadable,
        "question_type": question_type,
        # OCR never produced evidence for this page: stay fail-closed but
        # attribute the gate to OCR failure, not to slot identity drift.
        "slot_identity_error": "ocr_failed",
    }


def sync_artifact_slots(artifact: dict, questions: list[dict], *, materialize: bool = False) -> dict:
    """Bind saved geometry and schema without adding fields to legacy reads."""
    result = copy.deepcopy(artifact)
    by_no = {str(q.get("question_no") or q.get("question_number")): q for q in questions}
    schema = {str(q.get("question_number") or q.get("question_no")): q for q in result.get("question_schema") or []}
    for region in result.get("template_answer_regions") or []:
        number = str(region.get("question_number") or "")
        question = by_no.get(number)
        if question is None:
            continue
        count = len(region.get("regions") or [])
        raw_ids = region.get("slot_ids", question.get("slot_ids"))
        if raw_ids is None and (not materialize or count <= 1):
            continue
        ids = normalize_slot_ids(number, raw_ids if raw_ids is not None else make_slot_ids(number, count), count)
        expected = question.get("slot_ids")
        if expected is not None and expected != ids:
            # The slot ID is the box's identity, so a reordered region list is
            # the same binding (a box drawn for a kept position arrives last).
            # Restore contract order; only a different set of IDs is a conflict.
            if sorted(expected) != sorted(ids):
                raise ValueError(f"第 {number} 题作答框的槽编号与题目不一致，请重新核对该题作答框")
            by_id = dict(zip(ids, region["regions"]))
            region["regions"] = [by_id[slot] for slot in expected]
            ids = list(expected)
        if len(question.get("answer_slots") or []) != count:
            raise ValueError(f"第 {number} 题答案数量与作答区不一致，请逐槽核对")
        question["blank_count"] = count
        question["slot_ids"] = ids
        region["slot_ids"] = list(ids)
        if number in schema:
            schema[number]["slot_ids"] = list(ids)
        if question.get("region_kinds"):
            region["region_kinds"] = list(question["region_kinds"])
            if number in schema:
                schema[number]["region_kinds"] = list(question["region_kinds"])
    return result


def build_slot_evidence_prompt(base_prompt: str, questions: list[dict]) -> str:
    """One response schema: never append a competing slot format to a scalar example."""
    contract = slot_transcription_prompt(questions)
    if not contract:
        return base_prompt
    task, marker, old_output = base_prompt.partition("三、该返回什么")
    if not marker:
        raise ValueError("slot evidence prompt requires an explicit output section")
    start = old_output.find("{")
    if start < 0:
        raise ValueError("slot evidence prompt is missing its JSON example")
    _, end = json.JSONDecoder().raw_decode(old_output[start:])
    context = old_output[start + end:]
    task = "\n".join(line for line in task.splitlines() if not line.startswith("同一题若有多个独立作答框"))
    mapping = json.loads(contract.splitlines()[1].split("=", 1)[1])
    number = next(iter(mapping))
    example = {"questions": [{"question_number": number, "student_slots": [
        {"slot_id": mapping[number][0], "student_answer": "原始文字或算式",
         "answer_state": "visible|blank|unreadable"}]}]}
    return (task + "\n三、该返回什么\n单槽和多槽题格式相同，都返回student_slots。严格只返回JSON：\n"
            + json.dumps(example, ensure_ascii=False, indent=2) + context + contract)


def slot_transcription_prompt(questions: list[dict]) -> str:
    mapping = {}
    for question in questions:
        if question.get("slot_ids") is not None:
            number = str(question.get("question_no") or question.get("question_number") or "")
            expected = text_slot_ids(number, question)
            if expected:
                mapping[number] = expected
    if not mapping:
        return ""
    labels = [{"slot_id": slot_id, "label_text": "".join(slot_label_lines(number, slot_id)),
               "label_lines": slot_label_lines(number, slot_id)}
              for number, ids in mapping.items() for slot_id in ids]
    return (
        "\n逐槽转录合同=" + json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
        + '\n上述题目按图中槽位标签转录，每题返回 student_slots:[{"slot_id":"2[1]",'
        '"student_answer":"原始文字或算式","answer_state":"visible|blank|unreadable"}]。'
        '每个指定ID恰好一次；空答保留空字符串，不合并/顺延；作图位不转文字。'
        '无上述合同的题保持原格式。'
        '\n槽位标签对应表=' + json.dumps(labels, ensure_ascii=False, separators=(",", ":"))
        + '\n槽号默认横排，例如1.2表示1[2]；只有横排标签会侵入左邻作答框时才竖排，'
        '例如上行1、下行.2仍表示1[2]；层级编号按对应表读取，不是分数或学生作答。'
        '所有编号均为透明底、蓝字、蓝色空心小矩形框，只框编号，不绘制作答区大框。'
        '槽位标签紧贴自己作答框左侧，通常实色；竖排后仍压到前方作答框时，字和小框以65%不透明度显示；'
        '透出的笔迹仍是学生作答，按笔迹上下文判断归属，不按标签覆盖范围分配。'
        '\n横排槽位定位规则：先定位蓝色题号，再从该题号右侧沿水平方向寻找同一行作答。'
        '蓝色题号贴在自己作答区左边，不是答案末尾标记。'
        '题号左侧的已有笔迹，不可因离这个题号较近、被它覆盖或从它透出，就分给这个题号；'
        '先判断是否为左邻槽作答的延续，不把左邻答案挪到右侧空槽。'
        '保持同一串笔迹的数字、正负号、小数点和分数完整，不拼接相邻槽答案。'
        '这是定位起点和阅读方向，不是硬裁切边界；连贯笔迹的少量越界和下方续行仍完整保留。'
        '图片保留完整分栏；作答框仅用于定位和差分豁免，不是转录裁切边界，框外延伸笔迹也要读。'
        '返回原始slot_id，不把显示编号替换进slot_id；无法确定归属返回unreadable，不猜测。'
    )
