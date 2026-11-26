"""Teacher review decisions on a paper, applied answer box by answer box.

A folded multi-blank question is decided per answer box, by the ids of its
``grading_units`` (e.g. "1[3]", shown as 1.3 on the review image); every other
detail is one answer box decided by its question number. The question's own
verdict is refolded from its boxes, so counts, marks and points follow the
teacher's per-box verdicts.
"""
from __future__ import annotations

from services.scoring_engine import apply_teacher_box_verdict, apply_teacher_verdict


def decision_targets(details: list) -> dict[str, tuple[dict, int | None]]:
    """Every decision id of a paper -> (detail, box index, or None for a whole detail)."""
    targets: dict[str, tuple[dict, int | None]] = {}
    for detail in details or []:
        if not isinstance(detail, dict):
            continue
        number = str(detail.get("question_no") or detail.get("question_number") or "").strip()
        units = detail.get("grading_units") if isinstance(detail.get("grading_units"), list) else []
        boxes = [
            (str(unit.get("question_no") or f"{number}[{index + 1}]").strip(), index)
            for index, unit in enumerate(units) if isinstance(unit, dict)
        ]
        for target, box in boxes or [(number, None)]:
            if not number or not target or target in targets:
                raise ValueError("暂存题目编号无效，不能保存人工判定")
            targets[target] = (detail, box)
    return targets


def apply_review_decisions(details: list, verdicts: dict[str, str], *, complete: bool) -> list[tuple[str, str, str]]:
    """Apply {decision id: verdict}; return (id, old verdict, new verdict) for each change.

    ``complete`` requires a verdict for every answer box of the paper (saving);
    a preview may send only the boxes the teacher changed.
    """
    targets = decision_targets(details)
    if set(verdicts) - set(targets):
        raise ValueError("人工判定包含不属于该试卷的作答框")
    if complete and set(verdicts) != set(targets):
        raise ValueError("人工判定必须完整覆盖该学生的全部作答框")
    changes = []
    for target, verdict in verdicts.items():
        detail, box = targets[target]
        old = str((detail if box is None else detail["grading_units"][box]).get("result") or "needs_review")
        if old == verdict:
            continue
        if box is None:
            apply_teacher_verdict(detail, verdict)
        else:
            apply_teacher_box_verdict(detail, box, verdict)
        changes.append((target, old, verdict))
    return changes
