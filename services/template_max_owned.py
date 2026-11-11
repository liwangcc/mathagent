"""Parallel locator/solver contracts: shared slot IDs v4 and archived keys v3.

Flash v4 proposes the inventory; parallel Max results reconcile the draft.
V3 printed-key reception remains available only for explicit historical replay.
"""
from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from typing import NamedTuple

SLOT_SOURCE = "max_locator_v3"
SHARED_SLOT_SOURCE = "flash_slots_v4"
SLOT_SOURCES = {SLOT_SOURCE, SHARED_SLOT_SOURCE}
MAX_SLOTS_PER_QUESTION = 20
SHARED_GUIDE = "目录slots是共同作答位清单，逐项原样返回slot_id；cue只用于在原图中指认该处，内容以原图印刷为准。"
# Optional hint; absence keeps the printed-position behaviour.
UNMARKED_POSITION_GUIDE = (
    "只有一个文字作答位的选择题或填空题，若原图在该题没有印刷括号、横线、方框等填写位置"
    "（例如选择题只列选项、题干后没有括号；学生可能写在题号前、题末或圈选项），"
    "该题另返回\"answer_position\":\"unmarked\"，regions照常框最可能的作答处"
    "（填空题通常为题干最后一个字之后同一行的空白）；模板上已有手写答案时仍以完整覆盖手写为准。"
    "有印刷填写位置的题不返回answer_position。"
)


class MaxTemplateRejection(ValueError):
    """A model explicitly rejected the directory; retries cannot override it."""


def _shared_mode(manifest):
    flags = ["slots" in row for row in manifest]
    if any(flags) and not all(flags):
        raise ValueError("题目与作答位目录版本混用")
    return bool(flags) and all(flags)


KEY_GUIDE = (
    "答案位置用answer_key标识：part为该目录题目内原图印刷的小问编号（无则空字符串）；"
    "每块连续解答区用anchor为空字符串、blank_index=0，区内多个量写在同一个答案项中。"
    "独立填写位置：整题只有一个文字填写位置时用anchor为空字符串、blank_index=0；"
    "同题有多个独立填写位置时，anchor逐字记录所在整行印刷文字或算式，空位统一为□、分数用a/b、乘除用×÷；"
    "blank_index为同一part与anchor下的空位阅读序号，从1起。"
)


def _rows(value, field, expected):
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须为数组")
    result = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get(field), str):
            raise ValueError(f"{field} 身份无效")
        key = row[field]
        if key not in expected or key in result:
            raise ValueError(f"{field} 未知或重复")
        result[key] = row
    if set(result) != set(expected):
        raise ValueError(f"{field} 缺失")
    return result


def _text(value, *, empty=True, limit=16000):
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value.strip())):
        raise ValueError("模型文本字段无效")
    return value


def _strings(value):
    if not isinstance(value, list):
        raise ValueError("模型说明必须为字符串数组")
    return [_text(v) for v in value]


def _key(raw):
    if not isinstance(raw, dict) or set(raw) != {"part", "anchor", "blank_index"}:
        raise ValueError("答案位置键字段无效")
    _text(raw["part"], limit=128)
    anchor = _text(raw["anchor"], limit=2000)
    index = raw["blank_index"]
    if type(index) is not int or not 0 <= index <= 64 or bool(anchor.strip()) != (index > 0):
        raise ValueError("答案位置键的空位序号无效")
    return copy.deepcopy(raw)


def _key_token(key):
    # Formatting equivalence only: never rewrite variables, operators, values,
    # punctuation or truncate anchors to manufacture a semantic match.
    anchor = "".join(unicodedata.normalize("NFKC", key["anchor"]).split())
    return key["part"], anchor, key["blank_index"]


def _box(raw):
    if (not isinstance(raw, list) or len(raw) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or not 0 <= v <= 1000 for v in raw)
            or raw[0] >= raw[2] or raw[1] >= raw[3]):
        raise ValueError("Max 作答框坐标无效")
    return [float(v) for v in raw]


class LocatorFrame(NamedTuple):
    """How one locator request names the picture it carries and that picture's coordinates."""
    image: str       # what the contract calls the picture
    space: str       # what it calls a coordinate of that picture
    columns: str     # label of the column list
    definition: str  # the sentence that fixes origin, extent and grid


def locator_prompt(manifest, *, width, height, columns):
    """The whole-page prompt: one grid stamped on the page, page coordinates throughout."""
    return framed_locator_prompt(manifest, columns, LocatorFrame(
        image="原图", space="整页坐标", columns="分栏",
        definition=f"整页{width}×{height}，坐标normalized_1000_v1，网格使用整页坐标。"))


def framed_locator_prompt(manifest, columns, frame):
    """The locator contract worded for ``frame``; what must be returned does not depend on it."""
    shared = _shared_mode(manifest)
    placement = ("每个slots项对应一个regions项，slot_id沿用目录，区域包含该项完整作答位置及已有笔迹。"
                 if shared else "每个独立填写位置一个框；一题中并列的多道计算式逐式框选；连续演算区一个框。框包含完整作答位置及其中已有笔迹。")
    binding = (SHARED_GUIDE if shared else KEY_GUIDE + "drawing区域的answer_key为null。")
    region_example = ('{"slot_id":"1[1]","kind":"answer","bbox":[100,200,400,300]}'
                      if shared else '{"kind":"answer","bbox":[100,200,400,300],"answer_key":{"part":"","anchor":"","blank_index":0}}')
    rules = (
        "按目录在原图中定位学生需要完成的作答区域。" + frame.definition +
        "按目录逐项返回整题：question_number取q_id去掉Q后的编号；原图中该题号下的作答位置列在regions内。"
        "每题完整返回题型、印刷题号框、choice_mode和按阅读顺序排列的regions。"
        "question_type取choice、fill、solve或drawing，choice_mode取single、multiple或unknown。每题regions为1至20项，逐项沿用作答位，不截断。"
        "kind=answer表示文字、数字或选项填写区域，kind=drawing表示学生需新增绘制、涂色或标注的区域。"
        + placement +
        "结合题目目标和底图已有内容确定学生还需新增笔迹的作图任务；已表达题意的印刷示意属于题干条件。"
        "作图框保留完成判定所需的底图和参照。label_anchor为作图题的题号标记点[x,y]，必须是有效" + frame.space + "，且x不得大于第一个作图框的左边界；允许贴左边界，不允许落入作图区内部。涂色和图文混合题也遵守此规则；无作图时为null。不得移动或缩小作图框来迎合锚点要求；无法确定时列入needs_review。"
        + binding)
    reply = (
        '\n返回一个JSON对象，结构示例（坐标须按原图实测）：{"coordinate_space":"normalized_1000_v1","questions":['
        '{"question_number":"1","question_number_bbox":[50,180,80,210],'
        '"question_type":"fill","choice_mode":"unknown",'
        '"regions":[' + region_example + '],'
        '"label_anchor":null}],"needs_review":[]}'
        + ("\n作图项的kind为drawing，slot_id和bbox照常返回；有作图时label_anchor返回[x,y]，用于辅助显示题号。"
           "先核对目录项是否真实存在：确认某题号多余时保留question_number并返回status=extra和reason；"
           "确认某槽多余时保留slot_id并返回status=extra和reason，不用虚构bbox补齐；"
           "无法可靠定位且拒绝继续时返回status=refused和reason。extra/refused会转教师复核，处理前模板不能保存；正常drawing不会。"
           + UNMARKED_POSITION_GUIDE
           if shared else "")
    )
    # The contract text says 原图 for the request's picture; a frame may call it otherwise.
    # Only the wording is renamed, never the directory or the column list.
    rules, reply = (part.replace("原图", frame.image) for part in (rules, reply))
    return (rules
            + f"\n{frame.columns}：{json.dumps(columns, ensure_ascii=False, separators=(',', ':'))}"
            + f"\n目录：{json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))}"
            + reply)


def build_prompts(manifest, *, education_stage, locator=None):
    stages = {"primary": "小学数学", "junior": "初中数学", "senior": "高中数学"}
    if education_stage not in stages:
        raise ValueError("模板必须提供学段")
    directory = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    shared = _shared_mode(manifest)
    binding = SHARED_GUIDE if shared else KEY_GUIDE
    shared_slot_instruction = ""
    if shared:
        slot_inventory = [
            {"q_id": row["q_id"], "slot_ids": [slot["slot_id"] for slot in row["slots"]]}
            for row in manifest
        ]
        shared_slot_instruction = (
            "这是v4共享作答位合同。每个answers题目的answer_items必须严格覆盖对应清单中的每个slot_id，"
            "每个slot_id恰好出现一次；不得漏项、增项、重复、重编号或按位置省略。每个answer_items元素包含slot_id、answer和status；"
            "status取answered、drawing、extra或refused。普通文字答案用answered，不能确定答案时answer留空并在uncertain说明。"
            "学生新增画图、涂色、标注的作图位用drawing且answer为空字符串，不生成标准图或文字作图答案。"
            "answers题目也返回status：含文字作答用answered；整题只有作图用drawing，standard_answer为空字符串、solution_steps为空数组。"
            "混合题的standard_answer和solution_steps只包含文字或计算作答，不生成作图部分的标准内容。"
            "题干中的印刷示意图本身不是作图作答位；目录槽位确实多余时，该slot_id返回status=extra、answer为空并用reason说明。"
            "目录整道题号确实多余时，保留q_id并返回status=extra和reason；无法按目录可靠作答且拒绝继续时返回status=refused和reason。"
            "extra和refused会转教师复核，处理前模板不能保存；正常drawing留空不会；不能用drawing或空答案代替多余项报告。"
            '正常作图槽示例：{"slot_id":"1[1]","answer":"","status":"drawing"}；'
            '多余槽示例：{"slot_id":"1[1]","answer":"","status":"extra","reason":"该处是印刷示意图，没有新增作图要求"}；'
            '多余整题示例：{"q_id":"Q1","status":"extra","reason":"该编号不是独立题目"}。'
            "连续演算区的多个量合在所属槽位的answer中。逐题槽位清单如下："
            + json.dumps(slot_inventory, ensure_ascii=False, separators=(",", ":"))
            + "。"
        )
    drawing_instruction = (shared_slot_instruction +
                           "answer_items逐项覆盖全部slot_id；作图位置status=drawing、answer为空字符串；连续演算区的多个量合在该项答案中。"
                           if shared else "作图要求写入standard_answer，answer_items只列文字或算式填写位置的答案。")
    answer_example = ('{"slot_id":"1[1]","answer":"答案","status":"answered"}' if shared else
                      '{"answer_key":{"part":"","anchor":"","blank_index":0},"answer":"答案"}')
    solve = (
        f"按目录看完整原图独立解答{stages[education_stage]}印刷题，给出题目要求学生完成的标准答案与必要步骤。"
        "求解依据为印刷题文、可见图形条件及明确标注；学生手写和批改痕迹仅为已有作答。"
        "选择题先求结论，再核对对应原选项，answer使用选项字母；步骤说明该字母对应的选项内容。"
        + drawing_instruction + binding + "每题answer_items最多20项，逐项覆盖作答位，不截断。可见关键图形条件写入visual_facts，确实无法辨清处写入uncertain。"
        f"\n目录：{directory}"
        '\n返回一个JSON对象，answers逐题换行：{"answers":[{"q_id":"Q1",'
        + ('"status":"answered",' if shared else '') +
        '"standard_answer":"文字答案；纯作图留空","answer_items":['
        + answer_example + '],"solution_steps":[],"visual_facts":[],"uncertain":[]}]}'
    )
    routing = ""
    if locator is not None:
        types = [
            {"q_id": "Q" + row["question_number"],
             "question_type": row["question_type"],
             "choice_mode": row["choice_mode"]}
            for row in locator["questions"]
        ]
        routing = "定位Max已经确定以下作答方式；按此清单转录，不再从原图重新猜题型：" + json.dumps(
            types, ensure_ascii=False, separators=(",", ":")) + "。"
    text = (
        f"按目录读取{stages[education_stage]}印刷题文，先按作答方式分流，再转录正文。"
        + routing
        + "选择题的question_text为空字符串；其余题逐字转录印刷正文与算式，保留原式，作答空白写（）。"
        "算式用纯文本：分数写a/b、带分数写2 1/3、乘除用×÷、根号用√，不用LaTeX命令或$定界符。"
        "图示条件由独立读图分支处理，正文来源为印刷内容。确实无法辨清的正文写入uncertain。"
        "逐项返回全部q_id，覆盖目录中的每一道题。"
        f"\n目录：{directory}"
        '\n完整响应只返回一个JSON对象，questions是对象中的数组字段：{"questions":[{"q_id":"Q1","question_text":"正文文字与算式","uncertain":[]}]}'
    )
    return solve, text


def receive_locator(payload, manifest):
    _reject_max_disposition(payload, "questions", "question_number", "regions")
    if (not isinstance(payload, dict)
            or set(payload) != {"coordinate_space", "questions", "needs_review"}
            or payload["coordinate_space"] != "normalized_1000_v1"):
        raise ValueError("Max 定位返回字段或坐标系无效")
    shared = _shared_mode(manifest)
    identities = {row["q_id"][1:]: row for row in manifest}
    rows = _rows(payload["questions"], "question_number", identities)
    normalized = []
    for number in identities:
        row = rows[number]
        if set(row) - {"answer_position"} != {"question_number", "question_number_bbox", "question_type", "choice_mode", "regions", "label_anchor"}:
            raise ValueError("Max 定位题目字段无效")
        # Internal question_type and graphic answer_kind are separate axes.
        # Accept the model's explicit drawing label without losing its regions.
        question_type = "solve" if row["question_type"] == "drawing" else row["question_type"]
        if question_type not in {"choice", "fill", "solve"} or row["choice_mode"] not in {"single", "multiple", "unknown"}:
            raise ValueError("Max 定位题型无效")
        regions = row["regions"]
        if not isinstance(regions, list) or not 1 <= len(regions) <= MAX_SLOTS_PER_QUESTION:
            raise ValueError("Max 作答区域列表无效")
        expected_ids = [s["slot_id"] for s in identities[number]["slots"]] if shared else []
        if shared:
            by_id = _rows(regions, "slot_id", expected_ids)
            regions = [by_id[sid] for sid in expected_ids]
        boxes, kinds, keys, seen = [], [], [], set()
        for region in regions:
            fields = {"slot_id", "kind", "bbox"} if shared else {"kind", "bbox", "answer_key"}
            optional_fields = {"slot_bbox"} if shared else set()
            if (not isinstance(region, dict)
                    or set(region) - optional_fields != fields
                    or region["kind"] not in {"answer", "drawing"}):
                raise ValueError("Max 作答区域字段或类型无效")
            if "slot_bbox" in region and _box(region["slot_bbox"]) != _box(region["bbox"]):
                raise ValueError("Max 作答区域的slot_bbox与bbox不一致")
            key = region["slot_id"] if shared else region["answer_key"]
            if shared:
                key = None if region["kind"] == "drawing" else key
            elif region["kind"] == "drawing":
                if key is not None:
                    raise ValueError("作图区不绑定文字答案键")
            else:
                key = _key(key)
                token = _key_token(key)
                if token in seen:
                    raise ValueError("Max 定位答案位置键重复")
                seen.add(token)
            boxes.append(_box(region["bbox"]))
            kinds.append(region["kind"])
            keys.append(key)
        drawing = [b for b, k in zip(boxes, kinds) if k == "drawing"]
        anchor = row["label_anchor"]
        if drawing:
            from services.template_evidence import validate_drawing_schema_geometry
            validate_drawing_schema_geometry(number, drawing, anchor)
        elif anchor is not None:
            raise ValueError("普通文字题的作图标记必须为空")
        if question_type == "choice" and kinds != ["answer"]:
            raise ValueError("每道独立选择题对应一个文字答案槽")
        # Only a single text slot can sit before the number; other values or
        # shapes keep today's printed-position behaviour.
        unmarked = (row.get("answer_position") == "unmarked" and kinds == ["answer"]
                    and question_type in {"choice", "fill"})
        normalized.append({"question_number": number,
                           **({"answer_position": "unmarked"} if unmarked else {}),
                           "question_number_bbox": _box(row["question_number_bbox"]),
                           "question_type": question_type, "choice_mode": row["choice_mode"],
                           "regions": boxes, "region_kinds": kinds, "region_keys": keys,
                           "drawing_regions": drawing, "label_anchor": copy.deepcopy(anchor),
                           "requires_drawing": bool(drawing),
                           "answer_kind": "drawing" if len(drawing) == len(boxes) else "mixed" if drawing else "non_graphic",
                           "slot_ids": expected_ids if shared else [f"{number}[{i}]" for i in range(1, len(boxes) + 1)],
                           "answer_slot_count": len(boxes), "answer_area_count": kinds.count("answer")})
    return {"coordinate_space": "normalized_1000_v1", "questions": normalized,
            "needs_review": _strings(payload["needs_review"]),
            "slot_source": SHARED_SLOT_SOURCE if shared else SLOT_SOURCE}


def receive_solver(payload, manifest):
    _reject_max_disposition(payload, "answers", "q_id", "answer_items")
    if not isinstance(payload, dict) or set(payload) != {"answers"}:
        raise ValueError("独立解题必须返回 answers")
    shared = _shared_mode(manifest)
    identities = {row["q_id"]: row for row in manifest}
    rows = _rows(payload["answers"], "q_id", identities)
    answers = []
    for qid in identities:
        row = rows[qid]
        if set(row) - {"status", "reason"} != {"q_id", "standard_answer", "answer_items", "solution_steps", "visual_facts", "uncertain"}:
            raise ValueError("独立解题字段无效")
        # status=drawing is advisory. The locator owns slot kinds, and binding
        # never stages model text for a pure graphic; a stray step or text is
        # therefore no reason to discard and re-buy the whole response.
        if row.get("status", "answered") not in {"answered", "drawing"}:
            raise ValueError("独立解题状态无效")
        items = row["answer_items"]
        if not isinstance(items, list) or len(items) > MAX_SLOTS_PER_QUESTION:
            raise ValueError("独立解题答案项无效")
        expected_ids = [s["slot_id"] for s in identities[qid]["slots"]] if shared else []
        if shared:
            _rows(items, "slot_id", expected_ids)
        parsed, seen = [], set()
        for item in items:
            key_field = "slot_id" if shared else "answer_key"
            if not isinstance(item, dict) or set(item) - {"status", "reason"} != {key_field, "answer"}:
                raise ValueError("独立解题答案项字段无效")
            if item.get("status", "answered") not in {"answered", "drawing"}:
                raise ValueError("独立解题槽位状态无效")
            key = item["slot_id"] if shared else _key(item["answer_key"])
            token = key if shared else _key_token(key)
            if token in seen:
                raise ValueError("独立解题答案位置键重复")
            seen.add(token)
            parsed.append({key_field: key, "answer": _text(item["answer"]),
                           **({"status": item["status"]} if "status" in item else {})})
        answers.append({"q_id": qid, "standard_answer": _text(row["standard_answer"], empty=shared),
                        "answer_items": parsed, **({"slot_ids": expected_ids} if shared else {}),
                        "solution_steps": _strings(row["solution_steps"]),
                        "answer_variants": [], "answer_review_reasons": _strings(row["uncertain"]),
                        "solve_visual_context": {"facts": _strings(row["visual_facts"]), "relations": [],
                                                 "uncertain": _strings(row["uncertain"])}})
    return {"answers": answers, "directory_issues": [],
            "slot_source": SHARED_SLOT_SOURCE if shared else SLOT_SOURCE}


MAX_FLAG_STATUSES = frozenset({"extra", "refused"})


def _slot_order(value):
    return int(value.rsplit("[", 1)[1][:-1])


def _valid_slot_id(number, value):
    return isinstance(value, str) and re.fullmatch(re.escape(number) + r"\[[1-9][0-9]{0,2}\]", value) is not None


def _max_flag(status, reason, source, slot_id=None):
    """A review marker for an explicit report; never a deletion or an answer."""
    text = " ".join(str(reason).split())[:240] if isinstance(reason, (str, int, float)) else ""
    return {"scope": "slot" if slot_id else "question", **({"slot_id": slot_id} if slot_id else {}),
            "status": status, "reason": text, "source": source}


def _reject_provider_refusal(payload):
    """A response-level refusal carries no bindable rows; stop without retrying."""
    if isinstance(payload, dict) and payload.get("refusal"):
        raise MaxTemplateRejection("Max 拒绝作答，模板生成已阻断")


def _reject_max_disposition(payload, rows_field, identity_field, items_field):
    """Strict/historical receivers have no review path for explicit reports."""
    _reject_provider_refusal(payload)
    rows = payload.get(rows_field) if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        items = row.get(items_field)
        targets = [(row, row.get(identity_field))]
        if isinstance(items, list):
            targets.extend((item, item.get("slot_id")) for item in items if isinstance(item, dict))
        for item, identity in targets:
            if item.get("status") in MAX_FLAG_STATUSES:
                label = str(identity or "未标明编号")[:100]
                reason = str(item.get("reason") or "")[:240]
                message = "确认目录存在多余题号或槽位" if item["status"] == "extra" else "拒绝按当前目录作答"
                raise MaxTemplateRejection(f"Max {message}：{label}。{reason} 历史合同无法转教师复核，请核对原题与目录")


def _candidate_manifest(manifest, rows, field, items_field):
    """Validate identities before allowing Max to propose a different inventory.

    A blank answer is still an explicit slot, not a vote to delete it. Malformed
    or duplicate identities never qualify as a second matching proposal.
    """
    identities = {row["q_id"] if field == "q_id" else row["q_id"][1:]: row for row in manifest}
    indexed = _rows(rows, field, identities)
    result = []
    for identity, original in identities.items():
        items = indexed[identity].get(items_field)
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_SLOTS_PER_QUESTION:
            raise ValueError("Max 候选作答位清单无效")
        number = original["q_id"][1:]
        ids = [item.get("slot_id") if isinstance(item, dict) else None for item in items]
        if any(not _valid_slot_id(number, sid) for sid in ids) or len(set(ids)) != len(ids):
            raise ValueError("Max 候选作答位编号无效或重复")
        cues = {slot["slot_id"]: slot["cue"] for slot in original["slots"]}
        result.append({**original, "slots": [
            {"slot_id": sid, "cue": cues.get(sid, sid)} for sid in sorted(ids, key=_slot_order)
        ]})
    return result


def _split_locator_flags(payload, manifest):
    """Move explicit locator reports out of the strict geometry contract.

    Reported positions stay in the inventory as unlocated placeholders. A
    whole-question report, or one that leaves no locatable slot, keeps every
    listed position unlocated so the teacher can delete or draw it.
    """
    numbers = {row["q_id"][1:] for row in manifest}
    clean = copy.deepcopy(payload)
    flags, pending, whole = {}, {}, set()
    rows = clean.get("questions") if isinstance(clean, dict) else None
    if not isinstance(rows, list):
        return clean, flags, pending, whole
    kept_rows = []
    for row in rows:
        number = row.get("question_number") if isinstance(row, dict) else None
        if not isinstance(number, str) or number not in numbers:
            kept_rows.append(row)
            continue
        if row.get("status") in MAX_FLAG_STATUSES:
            flags.setdefault(number, []).append(_max_flag(row["status"], row.get("reason"), "locator"))
            whole.add(number)
            continue
        regions = row.get("regions")
        if isinstance(regions, list):
            located = []
            for region in regions:
                sid = region.get("slot_id") if isinstance(region, dict) else None
                if (isinstance(region, dict) and region.get("status") in MAX_FLAG_STATUSES
                        and _valid_slot_id(number, sid)):
                    if sid in pending.get(number, []):
                        raise ValueError("Max 定位重复报告同一作答位")
                    flags.setdefault(number, []).append(_max_flag(region["status"], region.get("reason"), "locator", sid))
                    pending.setdefault(number, []).append(sid)
                else:
                    located.append(region)
            if pending.get(number) and not located:
                whole.add(number)
                continue
            if pending.get(number) and not any(isinstance(region, dict) and region.get("kind") == "drawing"
                                               for region in located):
                # The anchor labelled the reported drawing; no located one remains.
                row["label_anchor"] = None
            row["regions"] = located
        kept_rows.append(row)
    clean["questions"] = kept_rows
    return clean, flags, pending, whole


def _unlocated_geometry(number, ids):
    """Placeholder for a question the locator reported instead of locating."""
    return {"question_number": number, "question_number_bbox": None,
            "question_type": "solve", "choice_mode": "unknown",
            "regions": [[] for _ in ids], "region_kinds": [None] * len(ids),
            "region_keys": [None] * len(ids), "drawing_regions": [], "label_anchor": None,
            "requires_drawing": False, "answer_kind": "non_graphic", "slot_ids": list(ids),
            "answer_slot_count": len(ids), "answer_area_count": len(ids)}


def receive_locator_candidates(payload, manifest):
    """Keep geometry/identity checks, but do not freeze Flash's slot count.

    Explicit extra/refused reports become teacher-review placeholders; only a
    response-level refusal stops the import because it has nothing to review.
    """
    _reject_provider_refusal(payload)
    if not _shared_mode(manifest):
        return receive_locator(payload, manifest)
    clean, flags, pending, whole = _split_locator_flags(payload, manifest)
    active = [row for row in manifest if row["q_id"][1:] not in whole]
    if active:
        proposal = _candidate_manifest(active, clean.get("questions") if isinstance(clean, dict) else None,
                                       "question_number", "regions")
        located = receive_locator(clean, proposal)
    else:
        if (not isinstance(clean, dict)
                or set(clean) != {"coordinate_space", "questions", "needs_review"}
                or clean["coordinate_space"] != "normalized_1000_v1" or clean["questions"] != []):
            raise ValueError("Max 定位返回字段或坐标系无效")
        located = {"coordinate_space": "normalized_1000_v1", "questions": [],
                   "needs_review": _strings(clean["needs_review"]), "slot_source": SHARED_SLOT_SOURCE}
    by_number = {row["question_number"]: row for row in located["questions"]}
    questions = []
    for row in manifest:
        number = row["q_id"][1:]
        reported = pending.get(number, [])
        if number in whole:
            ids = sorted({slot["slot_id"] for slot in row["slots"]} | set(reported), key=_slot_order)
            questions.append(_unlocated_geometry(number, ids))
            continue
        geometry = by_number[number]
        if reported:
            if set(reported) & set(geometry["slot_ids"]):
                raise ValueError("Max 定位同时报告并定位了同一作答位")
            entries = sorted(
                list(zip(geometry["slot_ids"], geometry["regions"], geometry["region_kinds"], geometry["region_keys"]))
                + [(sid, [], None, None) for sid in reported],
                key=lambda entry: _slot_order(entry[0]))
            geometry.update(slot_ids=[e[0] for e in entries], regions=[e[1] for e in entries],
                            region_kinds=[e[2] for e in entries], region_keys=[e[3] for e in entries],
                            answer_slot_count=len(entries))
        questions.append(geometry)
    located["questions"] = questions
    located["max_flags"] = flags
    return located


def _split_solver_flags(payload, manifest):
    """Replace explicit solver reports with blank, bindable positions."""
    slots = {row["q_id"]: [slot["slot_id"] for slot in row["slots"]] for row in manifest}
    clean = copy.deepcopy(payload)
    flags = {}
    rows = clean.get("answers") if isinstance(clean, dict) else None
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        qid = row.get("q_id") if isinstance(row, dict) else None
        if not isinstance(qid, str) or qid not in slots:
            continue
        number = qid[1:]
        if row.get("status") in MAX_FLAG_STATUSES:
            flags.setdefault(number, []).append(_max_flag(row["status"], row.get("reason"), "solver"))
            rows[index] = {"q_id": qid, "standard_answer": "",
                           "answer_items": [{"slot_id": sid, "answer": ""} for sid in slots[qid]],
                           "solution_steps": [], "visual_facts": [], "uncertain": []}
            continue
        items = row.get("answer_items")
        for position, item in enumerate(items if isinstance(items, list) else []):
            sid = item.get("slot_id") if isinstance(item, dict) else None
            if isinstance(item, dict) and item.get("status") in MAX_FLAG_STATUSES and _valid_slot_id(number, sid):
                flags.setdefault(number, []).append(_max_flag(item["status"], item.get("reason"), "solver", sid))
                items[position] = {"slot_id": sid, "answer": ""}
    return clean, flags


def receive_solver_candidates(payload, manifest):
    """Accept explicit empty answers and turn explicit reports into review flags."""
    _reject_provider_refusal(payload)
    if not _shared_mode(manifest):
        return receive_solver(payload, manifest)
    clean, flags = _split_solver_flags(payload, manifest)
    proposal = _candidate_manifest(manifest, clean.get("answers") if isinstance(clean, dict) else None,
                                   "q_id", "answer_items")
    result = receive_solver(clean, proposal)
    result["max_flags"] = flags
    return result


def max_flag_message(number, flag):
    target = f"作答位 {flag['slot_id']}" if flag.get("slot_id") else f"第 {number} 题"
    branch = "定位" if flag.get("source") == "locator" else "解题"
    verdict = "判定为目录多余" if flag.get("status") == "extra" else "拒绝按目录作答"
    reason = f"（{flag['reason']}）" if flag.get("reason") else ""
    return f"Max {branch}{verdict}：{target}{reason}，请教师删除或确认保留"


def _merged_flags(*sources):
    merged = {}
    for source in sources:
        for number, rows in (source or {}).items():
            for flag in rows:
                if flag not in merged.setdefault(number, []):
                    merged[number].append(copy.deepcopy(flag))
    return merged


def reconcile_slot_candidates(locator, solver, manifest):
    """Merge parallel proposals by identity, preserving disagreement for review.

    Equal, valid ID sets replace Flash's proposal. Otherwise use the larger
    Max proposal, preferring located slots on a tie. A position either branch
    explicitly reported as extra/refused is never dropped by that rule: it stays
    as a blocking teacher-review item. Missing geometry stays empty until a
    teacher draws it; missing answers are never recovered from list position
    or summary prose.
    """
    if not _shared_mode(manifest):
        return locator, solver
    located, solved = copy.deepcopy(locator), copy.deepcopy(solver)
    flags = _merged_flags(located.pop("max_flags", None), solved.pop("max_flags", None))
    answers = {answer["q_id"]: answer for answer in solved["answers"]}
    for geometry in located["questions"]:
        number = geometry["question_number"]
        answer = answers["Q" + number]
        locator_ids = geometry["slot_ids"]
        solver_ids = answer["slot_ids"]
        agreed = set(locator_ids) == set(solver_ids)
        retained = locator_ids if len(locator_ids) >= len(solver_ids) else solver_ids
        reported = {flag["slot_id"] for flag in flags.get(number, []) if flag.get("slot_id")}
        ids = sorted(set(retained) | (reported & (set(locator_ids) | set(solver_ids))), key=_slot_order)
        if not 1 <= len(ids) <= MAX_SLOTS_PER_QUESTION:
            raise ValueError("候选作答区域超过每题20项，请教师拆分题目")
        regions = {sid: (box, kind, key) for sid, box, kind, key in zip(
            locator_ids, geometry["regions"], geometry["region_kinds"], geometry["region_keys"])}
        by_id = {item["slot_id"]: item for item in answer["answer_items"]}
        missing_geometry = [sid for sid in ids if not regions.get(sid, ((),))[0]]
        if not agreed:
            answer["answer_review_reasons"].append("作答区域清单不一致，已保留候选区域，请教师核对")
        if missing_geometry:
            located["needs_review"].append(f"第 {number} 题有 {len(missing_geometry)} 个作答区域待教师框选")
        for sid in missing_geometry:
            kind = regions.get(sid, (None, None, None))[1] or (
                "drawing" if by_id.get(sid, {}).get("status") == "drawing" else "answer")
            regions[sid] = ([], kind, None if kind == "drawing" else sid)
        for flag in flags.get(number, []):
            message = max_flag_message(number, flag)
            answer["answer_review_reasons"].append(message)
            if flag["source"] == "locator":
                located["needs_review"].append(message)
        geometry.update(
            slot_ids=ids,
            regions=[regions[sid][0] for sid in ids],
            region_kinds=[regions[sid][1] for sid in ids],
            region_keys=[regions[sid][2] for sid in ids],
            answer_slot_count=len(ids),
        )
        kinds = geometry["region_kinds"]
        if kinds != ["answer"]:
            geometry.pop("answer_position", None)
        drawing = [box for box, kind in zip(geometry["regions"], kinds) if kind == "drawing" and box]
        geometry["drawing_regions"] = drawing
        geometry["requires_drawing"] = "drawing" in kinds
        if not drawing:
            geometry["label_anchor"] = None
        geometry["answer_area_count"] = kinds.count("answer")
        geometry["answer_kind"] = ("mixed" if "answer" in kinds else "drawing") if "drawing" in kinds else "non_graphic"
        answer["answer_items"] = [by_id.get(sid, {"slot_id": sid, "answer": ""}) for sid in ids]
        # Preserve explicit answers outside the selected inventory for the
        # existing unbound-items review record. Never shift them into blanks.
        answer["answer_items"].extend(item for sid, item in by_id.items() if sid not in ids)
        answer["slot_ids"] = ids
    located["max_flags"] = flags
    return located, solved


def bind_answers(locator, solver):
    """Join shared slot IDs (v4), or exact historical printed keys (v3)."""
    source = locator.get("slot_source")
    if source not in SLOT_SOURCES or solver.get("slot_source") != source:
        raise ValueError("物理槽与答案的合同版本不一致")
    shared = source == SHARED_SLOT_SOURCE
    located = {"Q" + q["question_number"]: q for q in locator["questions"]}
    result = copy.deepcopy(solver)
    if {a["q_id"] for a in result["answers"]} != set(located):
        raise ValueError("定位与解题题目身份不一致")
    for answer in result["answers"]:
        geometry = located[answer["q_id"]]
        if geometry["answer_kind"] == "drawing":
            # Pure graphic references are optional teacher work. Keep the
            # printed question, but never stage a model-generated solution.
            answer["standard_answer"] = ""
            answer["solution_steps"] = []
        items = answer.pop("answer_items")
        if shared and answer.get("slot_ids") != geometry["slot_ids"]:
            raise ValueError("定位与解题作答位清单不一致")
        by_key = {(item["slot_id"] if shared else _key_token(item["answer_key"])): (i, item)
                  for i, item in enumerate(items)}
        used, values, mapping = set(), [], []
        issues = answer["answer_review_reasons"]
        if (shared and geometry["answer_kind"] != "drawing" and geometry["question_type"] != "choice"
                and not answer["standard_answer"].strip()):
            # The shared contract accepts a blank so a pure graphic can bind;
            # a text-bearing question still needs its whole-question answer.
            issues.append("缺少整题标准答案，需教师补充")
        for sid, kind, key in zip(geometry["slot_ids"], geometry["region_kinds"], geometry["region_keys"]):
            if kind == "drawing":
                values.append("")
                if shared:
                    index, _item = by_key[sid]
                    used.add(index)
                    if _item["answer"].strip():
                        issues.append(f"作图区 {sid} 生成了文字答案，请核对区域类型")
                mapping.append({"slot_id": sid, "solver_index": index if shared else None, "method": "drawing"})
                continue
            match = by_key.get(sid if shared else _key_token(key))
            if match is None:
                values.append("")
                mapping.append({"slot_id": sid, "solver_index": None, "method": "unbound"})
                issues.append(f"作答位 {sid} 的印刷位置键未匹配到标准答案，请核对")
            else:
                index, item = match
                used.add(index)
                values.append(item["answer"])
                mapping.append({"slot_id": sid, "solver_index": index, "method": "slot_id" if shared else "printed_key"})
                if not item["answer"].strip():
                    issues.append(f"作答位 {sid} 缺少标准答案，请补充")
        unbound = [copy.deepcopy(item) for i, item in enumerate(items) if i not in used]
        if unbound:
            issues.append("解题存在未绑定的答案项，请核对作答框和答案")
        answer.update(answer_slots=values, slot_ids=list(geometry["slot_ids"]),
                      answer_slot_count=len(values),
                      answer_status="needs_review" if issues else "ready",
                      slot_binding={"version": source, "region_keys": copy.deepcopy(geometry["region_keys"]),
                                    "solver_items": items, "mapping": mapping, "unbound_items": unbound})
    return result
