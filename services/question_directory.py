"""Pure, authoritative question identities shared by template localization and solving.

No application configuration, model requests, storage or image processing lives here.
Printed labels are display metadata; only program-assigned Q IDs bind model answers.
Coordinates always refer to the complete current image, not a cropped column.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import unicodedata
from typing import Any

from services.grading_contract import merge_directory_leaf_score_layer

DIRECTORY_VERSION = "question_directory_v1"
COORDINATE_SPACE = "normalized_1000_v1"
_KINDS = {"section", "group", "question", "answer_item"}
_QUESTION_TYPES = {"choice", "fill", "solve"}
_ANSWER_KINDS = {"non_graphic", "drawing", "mixed"}
_STAGES = {"primary": "小学", "junior": "初中", "senior": "高中"}
_MAX_NODES = 180
_MAX_DEPTH = 8


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _object(payload: Any) -> dict:
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        payload = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _issues(value: Any, field: str) -> list:
    if not isinstance(value, list) or any(not isinstance(item, (str, dict)) for item in value):
        raise ValueError(f"{field} must be a list of review messages")
    return deepcopy(value)


def _coordinate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1000 and math.isfinite(value))


def _box(value: Any, field: str) -> list:
    if (not isinstance(value, list) or len(value) != 4
            or not all(_coordinate(v) for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError(f"Invalid normalized_1000_v1 box: {field}")
    return list(value)


def _boxes(value: Any, field: str) -> list:
    if not isinstance(value, list) or len(value) > _MAX_NODES:
        raise ValueError(f"Invalid region list: {field}")
    return [_box(box, field) for box in value]


def _point(value: Any, field: str) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or not all(_coordinate(v) for v in value):
        raise ValueError(f"Invalid normalized_1000_v1 point: {field}")
    return list(value)


def _printed_number(value: str | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).strip().strip(" .、。()")
    text = re.sub(r"^第|题$", "", text)
    if re.fullmatch(r"[0-9]+", text):
        number = int(text)
        return str(number) if number > 0 else None
    # NFKC covers circled numbers; Chinese labels remain display-exact in paths.
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits and digits[text] > 0:
        return str(digits[text])
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
        left, right = text.split("十")
        return str(digits.get(left, 1) * 10 + digits.get(right, 0))
    return None


def _drawing_fields(node: dict) -> dict:
    answer_kind = node.get("answer_kind", "non_graphic")
    if not isinstance(answer_kind, str) or answer_kind not in _ANSWER_KINDS:
        raise ValueError("Invalid answer_kind")
    regions = _boxes(node.get("drawing_regions", []), "drawing_regions")
    anchor = _point(node.get("label_anchor"), "label_anchor")
    if answer_kind == "non_graphic" and (regions or anchor is not None):
        raise ValueError("non_graphic cannot define drawing regions or a drawing anchor")
    return {"answer_kind": answer_kind, "drawing_regions": regions, "label_anchor": anchor}


def _drawing_flag(node: dict) -> bool | None:
    """Read the directory's own "this question wants work on a figure" verdict.

    Distinct from answer_kind, which is a localization result: the flag only
    decides whether the locator has to run. None means the model did not answer,
    which stays unknown and must not be read as "no drawing".
    """
    value = node.get("requires_drawing")
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("Invalid requires_drawing")
    return value


def _drawing_issues(leaf: dict) -> list:
    if leaf["answer_kind"] == "non_graphic":
        return []
    reasons = []
    if not leaf["drawing_regions"]:
        reasons.append("drawing_region_missing")
    if leaf["label_anchor"] is None:
        reasons.append("drawing_label_anchor_missing")
    # label_anchor is an auxiliary marker; the artifact renderer separately
    # checks whether the eventual label footprint is visible and non-occluding.
    # Its center may be above, inside, or beside the drawing region.
    return [{"reason": reason, "node_id": leaf["node_id"], "printed_path": leaf["printed_path"]}
            for reason in reasons]


def _unambiguous_paths(paths: list[list[str]]) -> bool:
    tuples = [tuple(path) for path in paths]
    if len(set(tuples)) != len(tuples):
        return False
    # A leaf must not accidentally become the ancestor of a different leaf.
    return not any(left != right and right[:len(left)] == left for left in tuples for right in tuples)


def parse_question_directory(payload: Any) -> dict:
    """Validate a structure-v2 tree and assign stable leaf Q IDs in reading order.

    A globally unambiguous printed question number stays flat, including ordinary
    high-school exams. Sections contribute to Q only when needed to distinguish
    restarted numbering. Unprinted standalone questions stay single leaves and
    receive a provisional sibling identity with an explicit teacher-review issue.
    """
    payload = _object(payload)
    if payload.get("coordinate_space") != COORDINATE_SPACE:
        raise ValueError("Unexpected directory coordinate system")
    roots = payload.get("nodes")
    if not isinstance(roots, list) or not roots:
        raise ValueError("Missing directory nodes")
    issues = _issues(payload.get("needs_review"), "needs_review")
    flat, leaves = [], []
    active = set()

    def visit_siblings(siblings, parent, path, numeric_path, kinds, context, depth):
        if depth > _MAX_DEPTH:
            raise ValueError("Excessive directory depth")
        explicit = []
        for node in siblings:
            if not isinstance(node, dict):
                raise ValueError("Directory nodes must be objects")
            label = node.get("printed_label")
            if label is not None and (not isinstance(label, str) or not label.strip()):
                raise ValueError("Invalid printed label")
            explicit.append(_printed_number(label))
        known = [number for number in explicit if number is not None]
        if len(set(known)) != len(known):
            raise ValueError("Duplicate sibling printed identity")
        allocated = set(known)
        for index, (node, number) in enumerate(zip(siblings, explicit), 1):
            if len(flat) >= _MAX_NODES or id(node) in active:
                raise ValueError("Excessive or cyclic directory")
            if not isinstance(node.get("kind"), str) or node["kind"] not in _KINDS:
                raise ValueError("Invalid node kind")
            children = node.get("children")
            if not isinstance(children, list):
                raise ValueError("children must be explicit")
            if node["kind"] == "answer_item" and children:
                raise ValueError("answer_item must be a leaf")
            if not children and node["kind"] not in {"question", "answer_item"}:
                raise ValueError("A heading/material is not a gradable leaf")
            summary = node.get("context_summary", "")
            if not isinstance(summary, str):
                raise ValueError("context_summary must be text")
            bbox = node.get("question_number_bbox", node.get("number_bbox"))
            if "number_bbox" in node and "question_number_bbox" in node and node["number_bbox"] != node["question_number_bbox"]:
                raise ValueError("Conflicting printed-number bbox aliases")
            bbox = None if bbox is None else _box(bbox, "question_number_bbox")
            question_type = node.get("question_type")
            if question_type is not None and (not isinstance(question_type, str) or question_type not in _QUESTION_TYPES):
                raise ValueError("Invalid question_type")
            if not children and question_type not in _QUESTION_TYPES:
                raise ValueError("Missing leaf question_type")
            choice_mode = node.get("choice_mode", "unknown")
            if not isinstance(choice_mode, str) or choice_mode not in {"single", "multiple", "unknown"}:
                raise ValueError("Invalid choice_mode")
            synthesized = number is None
            if synthesized:
                number = str(index)
                while number in allocated:
                    number = str(int(number) + 1)
                allocated.add(number)
            current_path = path + [node.get("printed_label")]
            current_numeric = numeric_path + [number]
            current_kinds = kinds + [node["kind"]]
            entry = {
                "node_id": f"n{len(flat) + 1:03d}", "parent_id": parent,
                "printed_label": node.get("printed_label"), "printed_path": current_path,
                "numeric_path": current_numeric, "kind": node["kind"],
                "context_summary": summary, "shared_context": list(context),
                "question_type": question_type, "choice_mode": choice_mode if question_type == "choice" else "unknown",
                "question_number_bbox": bbox, "number_bbox": deepcopy(bbox),
                "regions": _boxes(node.get("regions"), "regions"),
                "exemptions": _boxes(node.get("exemptions"), "exemptions"),
                "is_leaf": not children, "synthetic_label": synthesized,
                **_drawing_fields(node),
                "requires_drawing": _drawing_flag(node),
            }
            flat.append(entry)
            if synthesized and node["kind"] != "answer_item":
                issues.append({"reason": "unresolved_printed_label", "node_id": entry["node_id"], "printed_path": current_path})
            if not children:
                if question_type == "choice" and choice_mode == "unknown":
                    issues.append({"reason": "choice_mode_unknown", "node_id": entry["node_id"], "printed_path": current_path})
                if question_type == "solve" and bbox is None and entry["answer_kind"] == "non_graphic":
                    issues.append({"reason": "solve_printed_number_missing", "node_id": entry["node_id"], "printed_path": current_path})
                if not entry["regions"] and entry["answer_kind"] != "drawing":
                    issues.append({"reason": "answer_region_missing", "node_id": entry["node_id"], "printed_path": current_path})
                issues.extend(_drawing_issues(entry))
                entry["_non_section_path"] = [n for n, kind in zip(current_numeric, current_kinds) if kind != "section"]
                leaves.append(entry)
            active.add(id(node))
            if children:
                visit_siblings(children, entry["node_id"], current_path, current_numeric, current_kinds,
                               context + ([summary] if summary else []), depth + 1)
            active.remove(id(node))

    visit_siblings(roots, None, [], [], [], [], 0)
    compact_paths = [leaf["_non_section_path"] for leaf in leaves]
    use_compact = _unambiguous_paths(compact_paths)
    chosen_paths = compact_paths if use_compact else [leaf["numeric_path"] for leaf in leaves]
    if not _unambiguous_paths(chosen_paths):
        raise ValueError("Ambiguous canonical leaf Q identity")
    for leaf, parts in zip(leaves, chosen_paths):
        leaf["question_number"] = ".".join(parts)
        leaf["q_id"] = "Q" + leaf["question_number"]
        del leaf["_non_section_path"]
    return {"version": DIRECTORY_VERSION, "coordinate_space": COORDINATE_SPACE,
            "flat_compatible": all(len(parts) == 1 for parts in chosen_paths),
            "nodes": deepcopy(flat), "leaves": deepcopy(leaves), "needs_review": issues}


def _directory_leaves(directory: dict) -> list:
    if (not isinstance(directory, dict) or directory.get("version") != DIRECTORY_VERSION
            or directory.get("coordinate_space") != COORDINATE_SPACE):
        raise ValueError("Expected a parsed authoritative question directory")
    leaves = directory.get("leaves")
    if not isinstance(leaves, list) or not leaves:
        raise ValueError("Missing directory leaves")
    seen = set()
    for leaf in leaves:
        if not isinstance(leaf, dict):
            raise ValueError("Invalid directory leaf")
        number = leaf.get("question_number")
        if (not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", number)
                or leaf.get("q_id") != "Q" + number or number in seen):
            raise ValueError("Invalid or duplicate canonical leaf Q identity")
        seen.add(number)
    _issues(directory.get("needs_review"), "needs_review")
    return leaves


def bind_directory_answers(directory: dict, response: Any) -> list[dict]:
    """Bind strict Q-only solver answers, never pairing by position or repairing IDs.

    Output is in authoritative reading order; each item is already a grading leaf
    (blank_count=subquestion_count=1), even if its parent has several children.
    Structural metadata is copied from the directory, never accepted from solver.
    """
    leaves = _directory_leaves(directory)
    response = _object(response)
    issues = _issues(response.get("directory_issues"), "directory_issues")
    answers = response.get("answers")
    if not isinstance(answers, list):
        raise ValueError("Missing Q-only answers")
    if set(response) - {"answers", "directory_issues"}:
        raise ValueError("Q-only solver cannot redefine directory structure")
    expected = {leaf["q_id"]: leaf for leaf in leaves}
    allowed = {"q_id", "question_text", "standard_answer", "solution_steps", "answer_status", "answer_variants"}
    found = {}
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) - allowed:
            raise ValueError("Q-only solver tried to redefine structural metadata")
        qid = answer.get("q_id")
        if not isinstance(qid, str) or qid not in expected or qid in found:
            raise ValueError("Unknown or duplicate answer Q")
        if not isinstance(answer.get("answer_status"), str) or answer["answer_status"] not in {"ready", "needs_review"}:
            raise ValueError("Invalid answer_status")
        for key in ("question_text", "standard_answer"):
            if not isinstance(answer.get(key), str):
                raise ValueError(f"Missing solver text: {key}")
            if answer["answer_status"] == "ready" and not answer[key].strip():
                raise ValueError(f"Ready solver answer lacks {key}")
        steps = answer.get("solution_steps", [])
        if isinstance(steps, str):
            steps = [line for line in steps.splitlines() if line.strip()]
        if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
            raise ValueError("Invalid solution_steps")
        variants = answer.get("answer_variants", [])
        if not isinstance(variants, list) or any(not isinstance(value, str) for value in variants):
            raise ValueError("Invalid answer_variants")
        found[qid] = {**deepcopy(answer), "solution_steps": list(steps), "answer_variants": list(variants)}
    if set(found) != set(expected):
        raise ValueError("Answers do not cover all directory leaves")
    result = []
    for leaf in leaves:
        answer = found[leaf["q_id"]]
        review = bool(directory["needs_review"] or issues or answer["answer_status"] == "needs_review")
        result.append(merge_directory_leaf_score_layer({**deepcopy(leaf), **answer,
                       "question_no": leaf["question_number"],
                       "directory_node_id": leaf["node_id"],
                       "directory_parent_id": leaf["parent_id"],
                       "blank_count": 1, "subquestion_count": 1,
                       "directory_version": DIRECTORY_VERSION,
                       "directory_metadata": deepcopy(leaf),
                       "binding_source": "program_directory",
                       "answer_status": "needs_review" if review else "ready",
                       "needs_review": review,
                       "directory_issues": deepcopy(directory["needs_review"] + issues)}))
    return result


def bind_drawing_locations(directory: dict, payload: Any) -> dict:
    """Return a directory copy updated ONLY with localized drawing attributes.

    All Q IDs and their order must match the frozen directory. The model may not
    change regions reserved by other answers, hierarchy, question types or text.
    Full label-footprint collision checking belongs to the image renderer.
    """
    leaves = _directory_leaves(directory)
    payload = _object(payload)
    if payload.get("coordinate_space") != COORDINATE_SPACE:
        raise ValueError("Unexpected drawing coordinate system")
    if set(payload) - {"coordinate_space", "questions", "needs_review"}:
        raise ValueError("Drawing locator cannot redefine directory structure")
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Missing drawing questions")
    allowed = {"question_number", "answer_kind", "drawing_regions", "label_anchor"}
    if any(not isinstance(row, dict) or set(row) != allowed for row in questions):
        raise ValueError("Drawing locator must return only the fixed drawing contract")
    if [row["question_number"] for row in questions] != [leaf["question_number"] for leaf in leaves]:
        raise ValueError("Drawing question identities/order differ from directory")
    result = deepcopy(directory)
    result["needs_review"].extend(_issues(payload.get("needs_review"), "needs_review"))
    by_node = {node["node_id"]: node for node in result["nodes"]}
    for leaf, row in zip(result["leaves"], questions):
        values = _drawing_fields(row)
        verdict = leaf.get("requires_drawing")
        if verdict is True and values["answer_kind"] == "non_graphic":
            # Two model stages disagree about whether this answer needs a
            # figure. Keep the returned candidate, but never silently erase
            # the directory's explicit requirement or skip teacher review.
            result["needs_review"].append({
                "reason": "drawing_requirement_conflict",
                "node_id": leaf["node_id"], "printed_path": leaf.get("printed_path"),
            })
        if values["answer_kind"] != "non_graphic" and leaf["question_type"] != "solve":
            if verdict is False:
                # The directory declined a figure for this item, so turning it
                # into a drawing contradicts the frozen directory instead of
                # localizing it.
                raise ValueError("Drawing locator cannot change a non-solve answer's kind")
            if verdict is None:
                # No verdict was recorded: the item is localized, but rewriting
                # e.g. a choice item into a drawing one must reach the teacher.
                result["needs_review"].append({
                    "reason": "drawing_kind_without_directory_verdict",
                    "node_id": leaf["node_id"], "printed_path": leaf.get("printed_path"),
                })
        leaf.update(values)
        by_node[leaf["node_id"]].update(deepcopy(values))
        result["needs_review"].extend(_drawing_issues(leaf))
        if values["answer_kind"] == "drawing":
            result["needs_review"] = [issue for issue in result["needs_review"]
                                      if not (isinstance(issue, dict)
                                              and issue.get("reason") == "answer_region_missing"
                                              and issue.get("node_id") == leaf["node_id"])]
    return result


def _dimensions(width: int, height: int) -> str:
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
        raise ValueError("Image dimensions must be positive integer pixels")
    return f"本次整张输入图片实际尺寸：宽={width}，高={height}。"


def _stage(education_stage: str) -> str:
    if not isinstance(education_stage, str) or education_stage not in _STAGES:
        raise ValueError("Unknown mathematics education stage")
    return f"本次学段：{_STAGES[education_stage]}，学科：数学。"
