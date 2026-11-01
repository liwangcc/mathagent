"""Deterministic scoring-unit expansion and aggregation.

A printed question is not always a grading unit:
- solve questions expand into independently judged subquestions;
- a multi-blank fill question expands for judgment, then folds back into one
  question-level record and one correctness denominator;
- daily homework has no points; exams use the confirmed batch rubric.
"""

from __future__ import annotations

import copy
import math
import re
from collections import OrderedDict
from typing import Any

from services.review_marks import normalize_review_boxes
from models import sanitize_error_type_fields


_DEFAULT_SOLVE_SCORES = (5.0, 7.0, 8.0)
_PART_PATTERNS = (
    re.compile(r"[（(]\s*(\d{1,2})\s*[）)]"),
    re.compile(r"([①②③④⑤⑥⑦⑧⑨⑩])"),
)
_SLOT_SPLIT_RE = re.compile(r"\s*(?:；|;|\n|\r\n)\s*")
_CHILD_QUESTION_RE = re.compile(r"^(.+?)[（(\[]\s*(\d{1,2})\s*[）)\]]$")


def _safe_positive_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result > 0 else default


def _fill_slot_scores(entry: dict, blank_count: int, total_score: float) -> tuple[str, list[float | None]]:
    """Return the confirmed fill scoring mode and per-slot point allocation."""
    mode = str(entry.get("fill_scoring_mode") or "equal").strip().lower()
    if mode not in {"equal", "all_or_nothing", "custom"}:
        mode = "equal"
    if mode == "all_or_nothing":
        return mode, [None] * blank_count
    if mode == "custom":
        raw = entry.get("blank_scores") or []
        try:
            scores = [float(value) for value in raw]
        except (TypeError, ValueError):
            scores = []
        if (
            len(scores) == blank_count
            and all(math.isfinite(score) and score > 0 for score in scores)
            and math.isclose(sum(scores), total_score, rel_tol=0, abs_tol=0.0001)
        ):
            return mode, scores
        # Contracts are normally validated before this point.  Fallback is
        # deliberately deterministic if an old/corrupt batch is encountered.
        mode = "equal"
    return mode, [total_score / blank_count] * blank_count


def infer_subquestion_count(question_text: str, standard_answer: str = "") -> int:
    """Infer explicit numbered parts, defaulting to one indivisible solve unit."""
    text = f"{question_text or ''}\n{standard_answer or ''}"
    best = 0
    for pattern in _PART_PATTERNS:
        matches = pattern.findall(text)
        if not matches:
            continue
        if pattern is _PART_PATTERNS[0]:
            numeric = [int(item) for item in matches if str(item).isdigit()]
            best = max(best, max(numeric, default=0))
        else:
            best = max(best, len(dict.fromkeys(matches)))
    return min(max(best, 1), 20)


def infer_blank_count(question_text: str, answer_slots: list[str] | None = None) -> int:
    if answer_slots:
        return min(max(len(answer_slots), 1), 20)
    # Runs of underscores are the most stable signal emitted by OCR/templates.
    runs = re.findall(r"_{2,}|＿{2,}|﹏{2,}", question_text or "")
    return min(max(len(runs), 1), 20)


def split_answer_slots(standard_answer: str, blank_count: int) -> list[str]:
    parts = [part.strip() for part in _SLOT_SPLIT_RE.split(standard_answer or "") if part.strip()]
    return parts if len(parts) == blank_count else []


def split_student_slots(student_answer: str, blank_count: int) -> list[str]:
    if blank_count == 1:
        return [str(student_answer or "").strip()]
    parts = [part.strip() for part in _SLOT_SPLIT_RE.split(student_answer or "")]
    if not parts:
        return []

    numbered: dict[int, str] = {}
    unnumbered: list[str] = []
    for part in parts:
        match = re.match(r"^[（(\[]\s*(\d{1,2})\s*[）)\]]\s*(.*)$", part)
        if match:
            slot_index = int(match.group(1))
            answer = match.group(2).strip()
            if 1 <= slot_index <= blank_count:
                numbered.setdefault(slot_index, answer)
                continue
        unnumbered.append(part)

    if not numbered:
        return unnumbered

    slots = [""] * blank_count
    for slot_index, answer in numbered.items():
        slots[slot_index - 1] = answer
    for slot_index, answer in zip((i for i, value in enumerate(slots) if not value), unnumbered):
        slots[slot_index] = answer
    return slots


def _exam_subquestion_scores(entry: dict) -> list[tuple[int, float]]:
    raw = entry.get("subquestion_scores") or []
    scores: list[float] = []
    for item in raw:
        try:
            score = float(item)
        except (TypeError, ValueError):
            score = 0.0
        scores.append(score if math.isfinite(score) and score > 0 else 0.0)
    try:
        box_count = int(entry.get("_answer_box_count") or 0)
    except (TypeError, ValueError):
        box_count = 0
    if box_count > 1 and len(scores) != box_count:
        scores = []
    if not scores:
        count = box_count if box_count > 1 else (
            1 if entry.get("directory_node_id") else infer_subquestion_count(
                str(entry.get("question_text") or ""),
                str(entry.get("standard_answer") or ""),
            )
        )
        scores = list(_DEFAULT_SOLVE_SCORES[:count])
        if count > len(scores):
            scores.extend([_DEFAULT_SOLVE_SCORES[-1]] * (count - len(scores)))
    return [(index + 1, score) for index, score in enumerate(scores) if score > 0]


def _daily_subquestion_indexes(entry: dict) -> list[int]:
    try:
        count = int(entry.get("subquestion_count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        box_count = int(entry.get("_answer_box_count") or 0)
    except (TypeError, ValueError):
        box_count = 0
    if box_count > 1 and count <= 1:
        count = box_count
    if count <= 0:
        count = infer_subquestion_count(
            str(entry.get("question_text") or ""),
            str(entry.get("standard_answer") or ""),
        )
    return list(range(1, min(max(count, 1), 20) + 1))


def normalize_ocr_questions(questions: list[dict], answer_map: dict) -> list[dict]:
    """Collapse duplicate/child OCR rows into one confirmed parent question.

    OCR may emit one row per blank or one row per solve subquestion.  Scoring is
    defined by the teacher-confirmed parent contract, so counting those rows
    directly would duplicate both the denominator and the available points.
    """
    merged: OrderedDict[str, dict] = OrderedDict()
    answers: dict[str, list[tuple[int, int, str]]] = {}
    for index, raw_question in enumerate(questions, 1):
        question = dict(raw_question or {})
        raw_no = str(question.get("question_no") or question.get("question_number") or index).strip()
        parent_no = raw_no
        child_label = ""
        if raw_no not in answer_map:
            child_match = _CHILD_QUESTION_RE.match(raw_no)
            if child_match and child_match.group(1).strip() in answer_map:
                parent_no = child_match.group(1).strip()
                child_label = child_match.group(2)

        if "slot_ids" in (answer_map.get(parent_no) or {}):
            conflict = bool(child_label) or parent_no in merged
            if parent_no not in merged:
                question["question_no"] = parent_no
                merged[parent_no] = question
                answers[parent_no] = []
            if conflict:
                # A contracted response must contain exactly one parent row.
                # Discard all candidates instead of keeping the first arrival.
                merged[parent_no].update({
                    "student_slots": [], "student_answer": "[无法识别]",
                    "answer_state": "unreadable",
                    "slot_identity_error": "duplicate parent or child row for contracted question",
                })
            continue

        if parent_no not in merged:
            question["question_no"] = parent_no
            merged[parent_no] = question
            answers[parent_no] = []
        elif str(question.get("answer_state") or "").strip().lower() == "unreadable":
            # Preserve an unreadable child marker when OCR collapsed several
            # rows into one parent mixed/text item.
            merged[parent_no]["answer_state"] = "unreadable"
        answer = str(question.get("student_answer") or "").strip()
        if answer:
            labelled = f"({child_label}){answer}" if child_label else answer
            # OCR can return numbered fill rows in arbitrary order.  Preserve
            # those explicit positions; unnumbered rows retain source order.
            position = int(child_label) if child_label else 1000 + index
            answers[parent_no].append((position, index, labelled))

    for parent_no, question in merged.items():
        combined = "；".join(
            answer for _position, _source_index, answer in sorted(answers[parent_no])
        )
        if combined:
            question["student_answer"] = combined
    return list(merged.values())


def build_grading_units(questions: list[dict], answer_map: dict, category: str) -> list[dict]:
    """Expand OCR questions into stable judgment units."""
    is_exam = category == "exam"
    units: list[dict] = []
    for question in normalize_ocr_questions(questions, answer_map):
        parent_no = str(question.get("question_no") or "").strip()
        entry = answer_map.get(parent_no, {}) or {}
        if "slot_ids" in entry:
            from services.template_slots import bind_student_slots
            prior_identity_error = str(question.get("slot_identity_error") or "").strip()
            question = bind_student_slots(question, parent_no, entry)
            if prior_identity_error and question.get("slot_identity_error"):
                # The question already carries the more specific reason its
                # evidence is unusable (e.g. ocr_failed); keep that over the
                # generic gate message while remaining fail-closed.
                question["slot_identity_error"] = prior_identity_error
        qtype = str(entry.get("question_type") or question.get("question_type") or "solve").lower()
        if qtype not in {"choice", "fill", "solve"}:
            qtype = "solve"
        question_text = str(entry.get("question_text") or question.get("question_text") or "")
        student_answer = str(question.get("student_answer") or "")
        answer_state = str(question.get("answer_state") or "").strip().lower()
        standard_answer = str(entry.get("standard_answer") or "")
        answer_boxes = []
        answer_box_source = "student"
        for source in (question, entry):
            for key in ("answer_bboxes", "answer_bbox", "student_answer_bbox", "bbox", "regions"):
                answer_boxes = normalize_review_boxes(source.get(key))
                if answer_boxes:
                    break
            if answer_boxes:
                answer_box_source = str(source.get("answer_box_source") or "student").strip().lower()
                break
        # A directory leaf can own several physical answer boxes.  Keep the
        # printed hierarchy number stable, but expose the box count to the
        # scoring expansion below so each box gets its own judgment unit.
        box_count = len(answer_boxes)
        unit_entry = {**entry, "_answer_box_count": box_count}
        common = {
            "parent_question_no": parent_no,
            "question_type": qtype,
            "choice_mode": str(entry.get("choice_mode") or "unknown").strip().lower(),
            "question_text": question_text,
            "student_answer": student_answer,
            "answer_state": answer_state,
            "answer_variants": entry.get("answer_variants", []) or [],
            "solution_steps": entry.get("solution_steps", []) or [],
            "options": entry.get("options", []) or [],
            "subquestion_count": int(entry.get("subquestion_count") or 1),
            "answer_slots": entry.get("answer_slots", []) or [],
            "figure_fingerprint": entry.get("figure_fingerprint") or entry.get("diagram_fingerprint") or "",
            "figure_description": entry.get("figure_description") or entry.get("diagram_description") or "",
            "grading_method": entry.get("grading_method") or "",
            "answer_bboxes": answer_boxes,
            "answer_box_source": answer_box_source,
            "entry": unit_entry,
            "answer_box_count": box_count,
        }
        if "slot_ids" in entry:
            common["slot_ids"] = list(entry["slot_ids"])
            # Pure drawing legitimately has no textual slots. Text/mixed
            # contracts were already normalized by bind_student_slots above.
            common["student_slots"] = question.get("student_slots", [])
            if question.get("slot_identity_error"):
                common["slot_identity_error"] = question["slot_identity_error"]

        # A typed graphic item is one drawing/text two-side judgment, as in the
        # existing mixed contract. Physical slots are position metadata, not
        # seven copies of the same drawing assessment. Keep the full transcript
        # and rubric intact for source/transcript/rubric evidence hashes.
        region_kinds = entry.get("region_kinds") or []
        if "drawing" in region_kinds:
            from services.grading_contract import finalize_answer_spec_for_category
            finalize_answer_spec_for_category({**entry, "question_no": parent_no}, category)
            slot_context = "\n".join(
                (str(entry["slot_ids"][index - 1]) if "slot_ids" in entry else f"[{index}]") + " " + ("作图（由图形证据判定，不是未填文字）" if kind == "drawing" else str(answer))
                for index, (kind, answer) in enumerate(zip(region_kinds, entry["answer_slots"]), 1)
            )
            units.append({
                **common, "key": parent_no, "detail_question_no": parent_no,
                "kind": "question", "standard_answer": standard_answer,
                "max_score": _safe_positive_float(entry.get("max_score"), 5.0) if is_exam else None,
                "scope_hint": "只判断文字/计算部分；物理槽位编号不可重排，作图位不当缺答。\n" + slot_context,
            })
            continue

        if "slot_ids" in entry and qtype in {"fill", "solve"}:
            # The contract, not punctuation or response order, defines units.
            slot_ids = entry["slot_ids"]
            references = entry.get("answer_slots") or []
            if not slot_ids or len(references) != len(slot_ids):
                raise ValueError("answer_slots must align with slot_ids")
            total_score = _safe_positive_float(entry.get("max_score"), 5.0) if is_exam else None
            mode, scores = (
                _fill_slot_scores(entry, len(slot_ids), total_score)
                if is_exam else ("equal", [None] * len(slot_ids))
            )
            if qtype == "solve" and is_exam:
                # Score metadata stays aligned to contract positions, while
                # judgment identity uses the stable (possibly sparse) suffix.
                part_scores = dict(_exam_subquestion_scores({
                    **unit_entry, "_answer_box_count": len(slot_ids),
                }))
                scores = [part_scores.get(index + 1, 0.0) for index in range(len(slot_ids))]
            for position, slot in enumerate(question["student_slots"]):
                suffix = slot["slot_id"].rsplit("[", 1)[1][:-1]
                key = slot["slot_id"] if qtype == "fill" else (
                    parent_no if len(slot_ids) == 1 and suffix == "1" else f"{parent_no}({suffix})"
                )
                units.append({
                    **common,
                    **slot,
                    "key": key,
                    "detail_question_no": parent_no if qtype == "fill" else key,
                    "kind": "fill_blank" if qtype == "fill" else ("question" if key == parent_no else "subquestion"),
                    "subquestion_no": suffix if qtype == "solve" and key != parent_no else "",
                    "blank_index": position + 1,
                    "blank_count": len(slot_ids),
                    "standard_answer": str(references[position]),
                    "answer_bboxes": [answer_boxes[position]] if position < len(answer_boxes) else [],
                    "max_score": scores[position],
                    "fill_scoring_mode": mode,
                    "blank_scores": list(scores) if mode == "custom" else [],
                    "fill_total_score": total_score,
                    "scope_hint": f"只判断作答位 {slot['slot_id']}；不要混入其他作答位的正误。",
                })
            continue

        if qtype == "fill":
            answer_slots = [str(item).strip() for item in (entry.get("answer_slots") or []) if str(item).strip()]
            try:
                configured_count = int(entry.get("blank_count") or 0)
            except (TypeError, ValueError):
                configured_count = 0
            blank_count = max(
                configured_count or infer_blank_count(question_text, answer_slots),
                box_count,
            )
            blank_count = min(max(blank_count, 1), 20)
            if blank_count > 1:
                slots = answer_slots if len(answer_slots) == blank_count else split_answer_slots(standard_answer, blank_count)
                # If an old template has no separated answers, retain the full
                # confirmed answer as context for every slot instead of guessing.
                if len(slots) != blank_count:
                    slots = [standard_answer] * blank_count
                total_score = _safe_positive_float(entry.get("max_score"), 5.0) if is_exam else None
                fill_scoring_mode, slot_scores = (
                    _fill_slot_scores(entry, blank_count, total_score)
                    if total_score is not None else ("equal", [None] * blank_count)
                )
                student_slots = split_student_slots(student_answer, blank_count)
                for blank_index, slot_answer in enumerate(slots, 1):
                    slot_boxes = (
                        [answer_boxes[blank_index - 1]]
                        if blank_index <= len(answer_boxes) else answer_boxes
                    )
                    units.append({
                        **common,
                        "key": f"{parent_no}[{blank_index}]",
                        "detail_question_no": parent_no,
                        "kind": "fill_blank",
                        "blank_index": blank_index,
                        "blank_count": blank_count,
                        "student_answer": (
                            student_slots[blank_index - 1]
                            if blank_index <= len(student_slots) else ""
                        ),
                        "answer_bboxes": slot_boxes,
                        "standard_answer": slot_answer,
                        "max_score": slot_scores[blank_index - 1],
                        "fill_scoring_mode": fill_scoring_mode,
                        "blank_scores": [
                            float(score) for score in slot_scores if score is not None
                        ] if fill_scoring_mode == "custom" else [],
                        "fill_total_score": total_score,
                        "scope_hint": f"只判断第 {blank_index} 个空；不要把其他空的正误混入本空结论。",
                    })
                continue

        if qtype == "solve":
            parts = _exam_subquestion_scores(unit_entry) if is_exam else [
                (index, None) for index in _daily_subquestion_indexes(unit_entry)
            ]
            part_count = len(parts)
            solve_slots = [str(item).strip() for item in (entry.get("answer_slots") or []) if str(item).strip()]
            if len(solve_slots) != part_count:
                solve_slots = split_answer_slots(standard_answer, part_count)
            if len(solve_slots) != part_count:
                solve_slots = [standard_answer] * part_count
            student_slots = split_student_slots(student_answer, part_count)
            # One unnumbered solve question remains one ordinary unit; explicit
            # multipart questions become independent details and analytics events.
            for part_index, part_score in parts:
                key = parent_no if len(parts) == 1 and part_index == 1 else f"{parent_no}({part_index})"
                units.append({
                    **common,
                    "key": key,
                    "detail_question_no": key,
                    "kind": "subquestion" if len(parts) > 1 else "question",
                    "subquestion_no": str(part_index) if len(parts) > 1 else "",
                    "standard_answer": solve_slots[part_index - 1],
                    "student_answer": (
                        student_slots[part_index - 1]
                        if part_index <= len(student_slots) else ""
                    ) if part_count > 1 else student_answer,
                    "answer_bboxes": (
                        [answer_boxes[part_index - 1]]
                        if part_index <= len(answer_boxes) else answer_boxes
                    ),
                    "max_score": part_score,
                    "scope_hint": (
                        f"只判断第（{part_index}）小问；必须忽略其他小问的正误。"
                        if len(parts) > 1 else ""
                    ),
                })
            continue

        units.append({
            **common,
            "key": parent_no,
            "detail_question_no": parent_no,
            "kind": "question",
            "standard_answer": standard_answer,
            "max_score": _safe_positive_float(entry.get("max_score"), 5.0) if is_exam else None,
            "scope_hint": "",
        })
    return units


def llm_payload_for_unit(unit: dict) -> dict:
    # The frozen physical slot is the model identity from template through OCR
    # and grading, even for one-slot solve questions. ``key`` stays internal
    # for compatibility with already-stored scores and report detail numbers.
    model_question_no = unit["key"]
    if "slot_id" in unit:
        from services.template_slots import normalize_slot_ids
        model_question_no = normalize_slot_ids(
            unit["parent_question_no"], [unit["slot_id"]], 1,
        )[0]
        if model_question_no not in unit.get("slot_ids", []):
            raise ValueError("grading slot is not in the frozen template contract")
    scoped_text = unit["question_text"]
    if unit.get("scope_hint"):
        scoped_text = f"{scoped_text}\n【评分范围】{unit['scope_hint']}"
    return {
        "question_no": model_question_no,
        **({"slot_id": unit["slot_id"]} if "slot_id" in unit else {}),
        "question_text": scoped_text,
        "student_answer": unit["student_answer"],
        "standard_answer": unit["standard_answer"],
        "answer_variants": unit.get("answer_variants") or [],
        "solution_steps": unit.get("solution_steps") or [],
        **({"grading_method": unit["grading_method"]} if unit.get("grading_method") else {}),
    }


def _points_for_result(result: str, max_score: float | None) -> float | None:
    if max_score is None:
        return None
    if result == "correct":
        return max_score
    if result == "partial":
        return max_score * 0.5
    if result == "wrong":
        return 0.0
    return None


def _base_detail(unit: dict, judged: dict, category: str) -> dict:
    if unit.get("slot_identity_error"):
        # This is an evidence gate, never a verdict the model may override.
        identity_error = str(unit["slot_identity_error"]).strip()
        gate_message = (
            "本题所在页 OCR 未完成，未计分；请老师对照原卷补录该题"
            if identity_error == "ocr_failed"
            else "作答位身份无法确认，请老师对照原卷确认"
        )
        explanation = str(judged.get("explanation") or "")
        judged = {
            **judged, "result": "needs_review",
            "error_type": "ocr_failed" if identity_error == "ocr_failed" else "ocr_slot_identity_invalid",
            # Keep only the gate text, plus the diagnostic id the grader may
            # have appended to it; nothing else can replace the gate message.
            "explanation": explanation if explanation.startswith(gate_message) else gate_message,
        }
    result = str(judged.get("result") or "needs_review")
    max_score = float(unit["max_score"]) if category == "exam" and unit.get("max_score") is not None else None
    awarded = _points_for_result(result, max_score)
    detail = {
        "question_no": unit["detail_question_no"],
        "question_number": unit["detail_question_no"],
        "parent_question_no": unit["parent_question_no"],
        "subquestion_no": unit.get("subquestion_no") or "",
        "question": unit["question_text"],
        "student_answer": unit["student_answer"],
        "standard_answer": unit["standard_answer"],
        "correct_answer": unit["standard_answer"],
        "model_answer": judged.get("model_answer", "") or "",
        "result": result,
        "explanation": judged.get("explanation", "") or "",
        "hint": judged.get("hint", "") or "",
        "question_type": unit["question_type"],
        "choice_mode": unit.get("choice_mode") or "unknown",
        "answer_variants": unit.get("answer_variants") or [],
        "solution_steps": unit.get("solution_steps") or [],
        "options": unit.get("options") or [],
        "subquestion_count": int(unit.get("subquestion_count") or 1),
        "answer_slots": unit.get("answer_slots") or [],
        "figure_fingerprint": unit.get("figure_fingerprint") or "",
        "figure_description": unit.get("figure_description") or "",
        "grading_method": unit.get("grading_method") or "",
        "match_method": (
            "evidence_gate" if unit.get("slot_identity_error")
            else "ocr" if judged.get("error_type") == "ocr_unreadable"
            else "choice" if unit["question_type"] == "choice" else "llm"
        ),
        "max_score": round(max_score, 4) if max_score is not None else None,
        "awarded_score": round(awarded, 4) if awarded is not None else None,
    }
    for field in ("slot_id", "slot_ids", "student_slots", "slot_identity_error", "text_grading_context_hash"):
        if field in unit:
            detail[field] = copy.deepcopy(unit[field])
    if "slot_ids" in unit:
        detail["answer_state"] = unit.get("answer_state", "")
    if unit["entry"].get("region_kinds"):
        detail["region_kinds"] = list(unit["entry"]["region_kinds"])
    if unit.get("answer_bboxes"):
        detail["answer_bboxes"] = [list(box) for box in unit["answer_bboxes"]]
        detail["answer_bbox"] = list(unit["answer_bboxes"][0])
        if unit.get("answer_box_source"):
            detail["answer_box_source"] = unit["answer_box_source"]
    # Semantic error classification is retired. Technical diagnostics (OCR,
    # alignment, drawing/page gates) may still be attached by their owning
    # safety branch for teacher review.
    technical_error = str(sanitize_error_type_fields(judged).get("error_type") or "").strip()
    if technical_error:
        detail["error_type"] = technical_error
    if "drawing_assessment" in judged:
        detail["drawing_assessment"] = judged["drawing_assessment"]
        detail["answer_kind"] = unit["entry"].get("answer_kind")
        detail["drawing_reference_hash"] = (unit["entry"].get("drawing_reference") or {}).get("reference_hash", "")
        detail["drawing_policy"] = judged["drawing_assessment"].get("policy")
        detail["match_method"] = "drawing" if detail["answer_kind"] == "drawing" else "drawing_and_text"
        if "text_assessment" in judged:
            detail["text_assessment"] = sanitize_error_type_fields(judged["text_assessment"])
    if "grading_receipt" in judged:
        detail["grading_receipt"] = judged["grading_receipt"]
    return detail


def _fold_fill_blanks(units: list[dict], judgments: dict[str, dict], category: str) -> dict:
    slot_details = [_base_detail(unit, judgments[unit["key"]], category) for unit in units]
    results = [slot["result"] for slot in slot_details]
    if any(result == "needs_review" for result in results):
        aggregate = "needs_review"
    elif all(result == "correct" for result in results):
        aggregate = "correct"
    elif all(result == "wrong" for result in results):
        aggregate = "wrong"
    else:
        aggregate = "partial"
    first = slot_details[0]
    parent_fields = dict(first)
    if "slot_id" in first:
        # The parent is the whole question, not an alias of its first slot.
        # Preserve blank positions and multiline text in the display/archive.
        parent_fields.pop("slot_id", None)
        parent_fields["student_answer"] = "；".join(slot["student_answer"] for slot in slot_details)
        states = [slot.get("answer_state") for slot in slot_details]
        parent_fields["answer_state"] = (
            "unreadable" if "unreadable" in states
            else "visible" if "visible" in states else "blank"
        )
    fill_scoring_mode = str(units[0].get("fill_scoring_mode") or "equal")
    entry_total = units[0].get("fill_total_score")
    if category == "exam" and fill_scoring_mode == "all_or_nothing":
        max_score = float(entry_total or 0)
        awarded = None if aggregate == "needs_review" else (max_score if aggregate == "correct" else 0.0)
    else:
        max_score = sum(float(slot["max_score"] or 0) for slot in slot_details) if category == "exam" else None
        awarded = None
        if category == "exam" and aggregate != "needs_review":
            awarded = sum(float(slot["awarded_score"] or 0) for slot in slot_details)
    explanations = [slot["explanation"] for slot in slot_details if slot["explanation"]]
    def feedback(field: str) -> str:
        # Keep every returned fragment tied to its own blank; never synthesize it.
        return "\n".join(
            (f"{slot.get('slot_id') or f'第{index + 1}空'}：" if len(slot_details) > 1 else "") + slot[field]
            for index, slot in enumerate(slot_details) if slot[field]
        )

    answer_bboxes = []
    for slot in slot_details:
        for box in slot.get("answer_bboxes") or []:
            if box not in answer_bboxes:
                answer_bboxes.append(box)
    # Preserve the same unit identity used by the grader and the visual
    # review renderer.  The parent detail remains available for legacy
    # consumers, while each physical answer box keeps its own verdict.
    grading_units = [
        {
            "question_no": unit["key"],
            **({"slot_id": unit["slot_id"]} if "slot_id" in unit else {}),
            "result": slot["result"],
            "answer_bboxes": [list(box) for box in (slot.get("answer_bboxes") or [])],
            "blank_index": index + 1,
        }
        for index, (unit, slot) in enumerate(zip(units, slot_details))
    ]
    return {
        **parent_fields,
        "question_no": first["parent_question_no"],
        "question_number": first["parent_question_no"],
        "subquestion_no": "",
        "standard_answer": str(units[0]["entry"].get("standard_answer") or ""),
        "correct_answer": str(units[0]["entry"].get("standard_answer") or ""),
        "result": aggregate,
        "explanation": "；".join(explanations),
        "hint": feedback("hint"),
        "model_answer": feedback("model_answer"),
        "blank_count": len(slot_details),
        "fill_scoring_mode": fill_scoring_mode,
        "blank_scores": list(units[0].get("blank_scores") or []),
        "blank_results": [
            {
                "blank_index": index + 1,
                **({
                    "slot_id": slot["slot_id"],
                    "student_answer": slot["student_answer"],
                    "answer_state": slot["answer_state"],
                } if "slot_id" in slot else {}),
                "result": slot["result"],
                "standard_answer": slot["standard_answer"],
                "explanation": slot["explanation"],
                "hint": slot["hint"],
                "model_answer": slot["model_answer"],
                "max_score": slot.get("max_score"),
                "awarded_score": slot.get("awarded_score"),
            }
            for index, slot in enumerate(slot_details)
        ],
        "grading_units": grading_units,
        **({"answer_bboxes": answer_bboxes, "answer_bbox": answer_bboxes[0]} if answer_bboxes else {}),
        "max_score": round(max_score, 4) if max_score is not None else None,
        "awarded_score": round(awarded, 4) if awarded is not None else None,
    }


def aggregate_grading_result(
    units: list[dict], judgments: dict[str, dict], category: str, student_name: str,
) -> dict:
    """Fold judgments into persisted details and category-specific aggregates."""
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for unit in units:
        if unit["key"] not in judgments:
            judgments[unit["key"]] = {
                "result": "needs_review",
                "explanation": "批改失败，请老师人工判断",
                "hint": "",
                "model_answer": "",
                "standard_answer": "",
            }
        group_key = f"fill:{unit['parent_question_no']}" if unit["kind"] == "fill_blank" else f"unit:{unit['key']}"
        grouped.setdefault(group_key, []).append(unit)

    details: list[dict] = []
    for group_units in grouped.values():
        if group_units[0]["kind"] == "fill_blank":
            details.append(_fold_fill_blanks(group_units, judgments, category))
        else:
            unit = group_units[0]
            details.append(_base_detail(unit, judgments[unit["key"]], category))

    counts = {"correct": 0, "partial": 0, "wrong": 0, "needs_review": 0}
    for detail in details:
        verdict = detail.get("result")
        counts[verdict if verdict in counts else "needs_review"] += 1
    total = len(details)
    pending = counts["needs_review"]
    correct_rate = None if pending else round(counts["correct"] / max(total, 1), 4)
    if category == "exam":
        max_score = round(sum(float(detail.get("max_score") or 0) for detail in details), 4)
        score = None if pending else round(sum(float(detail.get("awarded_score") or 0) for detail in details), 4)
    else:
        max_score = None
        score = None

    return {
        "student_name": student_name,
        "score": score,
        "max_score": max_score,
        "correct_rate": correct_rate,
        "scoring_version": 2,
        "correct_count": counts["correct"],
        "partial_count": counts["partial"],
        "wrong_count": counts["wrong"],
        "needs_review_count": pending,
        "total": total,
        "details": details,
    }


def _recalculated_detail_points(detail: dict, verdict: str, max_score: float) -> float | None:
    """Apply fill-specific rules when a teacher review rebuilds totals."""
    if str(detail.get("question_type") or "") == "fill":
        mode = str(detail.get("fill_scoring_mode") or "equal")
        if mode == "all_or_nothing":
            if verdict == "needs_review":
                return None
            return max_score if verdict == "correct" else 0.0
        if mode in {"custom", "equal"} and verdict == "partial":
            slots = detail.get("blank_results") or []
            allocated = 0.0
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                slot_max = slot.get("max_score")
                if slot_max is None:
                    continue
                try:
                    score = float(slot_max)
                except (TypeError, ValueError):
                    continue
                slot_points = _points_for_result(str(slot.get("result") or "needs_review"), score)
                allocated += float(slot_points or 0)
            # A teacher may newly select "partial" on an old all-wrong record
            # without per-blank edits; retain the conventional half-score then.
            if 0 < allocated < max_score:
                return allocated
    return _points_for_result(verdict, max_score)


def recalculate_result_from_details(result: dict, category: str) -> None:
    """Rebuild mutable review aggregates after teacher verdict changes.

    v2 exam details carry immutable ``max_score`` snapshots.  Daily
    records deliberately never expose a numeric score; partial is diagnostic
    and counts as not fully correct in ``correct_rate``.
    """
    details = [
        detail for detail in (result.get("details") or [])
        if isinstance(detail, dict) and detail.get("result") != "void"
    ]
    counts = {"correct": 0, "partial": 0, "wrong": 0, "needs_review": 0}
    for detail in details:
        verdict = str(detail.get("result") or "needs_review")
        verdict = verdict if verdict in counts else "needs_review"
        counts[verdict] += 1
        max_score = detail.get("max_score")
        if category == "exam" and max_score is not None:
            try:
                normalized_max = max(float(max_score), 0.0)
            except (TypeError, ValueError):
                normalized_max = 0.0
            detail["max_score"] = round(normalized_max, 4)
            points = _recalculated_detail_points(detail, verdict, normalized_max)
            detail["awarded_score"] = round(points, 4) if points is not None else None
        else:
            detail["max_score"] = None
            detail["awarded_score"] = None

    total = len(details)
    pending = counts["needs_review"]
    result.update({
        "total": total,
        "correct_count": counts["correct"],
        "partial_count": counts["partial"],
        "wrong_count": counts["wrong"],
        "needs_review_count": pending,
        "correct_rate": None if pending else round(counts["correct"] / max(total, 1), 4),
        "scoring_version": 2,
    })
    if category == "exam":
        result["max_score"] = round(sum(float(detail.get("max_score") or 0) for detail in details), 4)
        result["score"] = None if pending else round(
            sum(float(detail.get("awarded_score") or 0) for detail in details), 4,
        )
    else:
        result["max_score"] = None
        result["score"] = None
