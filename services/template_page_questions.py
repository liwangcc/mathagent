"""The question entries, anchors and answer regions a template page stores.

A page's located directory (``question_directory_v1`` leaves with their
geometry) and its bound answers (one row per ``q_id``) are joined leaf by leaf:
one printed question becomes one entry. Whether an entry awaits the teacher is
decided here, from the page's and the answer's review reasons.
"""
from __future__ import annotations

import copy

from services.grading_contract import merge_directory_leaf_score_layer


def assemble(directory: dict, locator: dict, solver: dict) -> tuple[list, list, list, list, list]:
    """``(questions, anchors, answer_regions, handwriting_regions, pending)`` of one page.

    ``locator`` is the reconciled locator result and ``solver`` the bound
    answers; ``pending`` lists the question numbers that await the teacher.
    """
    answers_by_id = {row["q_id"]: row for row in solver["answers"]}
    questions = []
    anchors, answer_regions, handwriting_regions = [], [], []
    pending = []
    for leaf in directory["leaves"]:
        answer = answers_by_id[leaf["q_id"]]
        review = bool(directory["needs_review"] or solver.get("directory_issues") or answer["answer_status"] == "needs_review")
        question = merge_directory_leaf_score_layer({**copy.deepcopy(leaf), **answer, "question_no": leaf["question_number"], "directory_node_id": leaf["node_id"],
                    "directory_parent_id": leaf["parent_id"],
                    # The frozen directory leaf remains one printed question;
                    # scoring expands it later from the persisted answer boxes.
                    "blank_count": int(leaf.get("answer_slot_count") or len(leaf.get("regions") or []) or 1), "subquestion_count": 1,
                    "answer_area_count": int(leaf["answer_area_count"]),
                    "drawing_area_count": int(leaf.get("drawing_area_count") or 0),
                    "region_kinds": list(leaf.get("region_kinds") or []),
                    "answer_slots": list(answer.get("answer_slots") or []),
                    "slot_ids": list(leaf.get("slot_ids") or []),
                    "answer_bboxes": [[round(v) for v in box] for box in leaf.get("regions", [])],
                    "directory_version": directory["version"], "directory_metadata": copy.deepcopy(leaf),
                    "binding_source": "flash_hierarchy_qwen_parallel", "answer_status": "needs_review" if review else "ready",
                    "needs_review": review, "directory_issues": copy.deepcopy(directory["needs_review"] + solver.get("directory_issues", [])
                                                                                 + answer.get("answer_review_reasons", []))})
        # Explicit Max extra/refused reports are draft-only blocking reviews;
        # confirmation requires the teacher to delete or keep each position.
        if locator.get("max_flags", {}).get(leaf["question_number"]):
            question["max_flags"] = copy.deepcopy(locator["max_flags"][leaf["question_number"]])
        questions.append(question)
        anchors.append({"question_number": leaf["question_number"], **({"question_number_bbox": [round(v) for v in leaf["question_number_bbox"]]} if leaf.get("question_number_bbox") else {})})
        # A solver-only slot has no trustworthy box. Keep its answer position
        # in the draft, and expose only real boxes as editable candidates.
        physical = [(sid, kind, box) for sid, kind, box in zip(
            leaf["slot_ids"], leaf["region_kinds"], leaf["regions"]) if box]
        answer_regions.append({"question_number": leaf["question_number"],
                               "regions": [[round(v) for v in box] for _, _, box in physical],
                               **({"region_kinds": [kind for _, kind, _ in physical],
                                   "slot_ids": [sid for sid, _, _ in physical]} if physical else {})})
        handwriting = [box for box in leaf.get("regions", []) if box]
        handwriting += [box for box in leaf.get("drawing_regions", []) if box not in handwriting]
        handwriting_regions.append({"question_number": leaf["question_number"],
                                    "regions": [[round(v) for v in box] for box in handwriting]})
        if review:
            pending.append(leaf["question_number"])
    return questions, anchors, answer_regions, handwriting_regions, pending
