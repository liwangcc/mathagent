"""Separate complete teacher references from the sparse text-grading task set."""
from collections import defaultdict
from copy import deepcopy
import re

from services.template_slots import normalize_slot_ids


TEXT_REFERENCE_INSTRUCTIONS = (
    "教师资料按整题提供完整题干和参考，answers按作答位题号关联；"
    "仅对学生answers中的题号判分，其余资料只作上下文。"
)
_TEXT_FIELDS = ("question_text", "standard_answer", "grading_method", "figure_description")
_LIST_FIELDS = ("answer_variants", "solution_steps", "options")


def build_teacher_material(answer_map: dict, units: list[dict]) -> list[dict]:
    """Project confirmed text only; never filter references by student state.

    Frozen templates supply the complete slot map, including slots not pending
    text grading. Legacy units supply their existing IDs without inventing or
    renumbering slots. Geometry, images and student evidence stay out of this
    projection. The original whole-question answer and shared steps remain
    available alongside the exact per-unit references.
    """
    from services.scoring_engine import llm_payload_for_unit

    by_parent = defaultdict(list)
    for unit in units:
        by_parent[unit["parent_question_no"]].append(unit)
    material = []
    for parent in dict.fromkeys([*answer_map, *by_parent]):
        group = by_parent.get(parent, [])
        entry = answer_map.get(parent) or {}
        first = group[0] if group else {}
        item = {"question_no": parent, "question_type": entry.get("question_type") or first.get("question_type") or "solve"}
        for field in _TEXT_FIELDS:
            value = entry.get(field) or first.get(field) or ""
            if value or field in {"question_text", "standard_answer"}:
                item[field] = deepcopy(value)
        for field in _LIST_FIELDS:
            value = entry.get(field) or first.get(field) or []
            if value:
                item[field] = deepcopy(value)

        references = {}
        if "slot_ids" in entry:
            answers = entry.get("answer_slots") or []
            ids = normalize_slot_ids(parent, entry["slot_ids"], len(answers))
            kinds = entry.get("region_kinds") or ["answer"] * len(ids)
            if len(kinds) != len(ids) or any(kind not in {"answer", "drawing"} for kind in kinds):
                raise ValueError("region_kinds must align with reference slots")
            for slot, answer, kind in zip(ids, answers, kinds):
                references[slot] = {"standard_answer": answer}
                if kind == "drawing":
                    references[slot]["scope_hint"] = "drawing"
        for unit in group:
            payload = llm_payload_for_unit(unit)
            number = payload["question_no"]
            ref = {"standard_answer": payload["standard_answer"]}
            if number in references and references[number]["standard_answer"] != ref["standard_answer"]:
                raise ValueError("grading reference differs from the frozen slot answer")
            if unit.get("scope_hint"):
                ref["scope_hint"] = unit["scope_hint"]
            if entry.get("answer_kind") == "mixed" or "drawing" in (entry.get("region_kinds") or []):
                ref["scope_hint"] = "text_only"
            references[number] = ref
        if not references:
            references[parent] = {"standard_answer": item["standard_answer"]}
        item["answers"] = references
        material.append(item)
    return material


def validate_teacher_material(material: list[dict], questions: list[dict]) -> list[dict]:
    """Require unique explicit reference bindings; extra context is not a task."""
    if not isinstance(material, list) or not material:
        raise ValueError("Teacher material must be a nonempty list")
    parents, references = set(), {}
    for item in material:
        if not isinstance(item, dict) or set(item) - {
            "question_no", "question_type", "answers", *_TEXT_FIELDS, *_LIST_FIELDS,
        }:
            raise ValueError("Invalid teacher material fields")
        parent = item.get("question_no")
        if not isinstance(parent, str) or not parent.strip() or parent in parents:
            raise ValueError("Missing or duplicate teacher question number")
        parents.add(parent)
        for field in _TEXT_FIELDS:
            if field in item and not isinstance(item[field], str):
                raise ValueError("Teacher rubric fields must be strings")
        for field in _LIST_FIELDS:
            values = item.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("Teacher rubric lists must contain text")
        answers = item.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise ValueError("Teacher references require explicit answer IDs")
        for number, ref in answers.items():
            if not isinstance(number, str) or not number.strip() or number in references:
                raise ValueError("Missing or duplicate teacher answer ID")
            if number != parent:
                if number.startswith(parent + "["):
                    normalize_slot_ids(parent, [number], 1)
                elif not re.fullmatch(re.escape(parent) + r"\([1-9][0-9]*\)", number):
                    raise ValueError("Teacher answer ID does not belong to its parent question")
            if not isinstance(ref, dict) or set(ref) - {"standard_answer", "scope_hint"}:
                raise ValueError("Invalid teacher answer fields")
            if not isinstance(ref.get("standard_answer"), str) or (
                "scope_hint" in ref and not isinstance(ref["scope_hint"], str)
            ):
                raise ValueError("Teacher answers must be text")
            references[number] = ref
    pending = set()
    for question in questions:
        number = question.get("question_no")
        if not isinstance(number, str) or number in pending or number not in references:
            raise ValueError("Pending answer ID is missing or duplicated in teacher material")
        pending.add(number)
        if question.get("standard_answer", "") != references[number]["standard_answer"]:
            raise ValueError("Pending answer reference does not match teacher material")
        if question.get("scope_hint") and question["scope_hint"] != references[number].get("scope_hint"):
            raise ValueError("Pending answer scope does not match teacher material")
    return deepcopy(material)
