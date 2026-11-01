"""Shared fixtures for the answer-sheet redesign tests (slices 1-6).

Everything here is built through the real contracts: a detailed-grading
``TemplateSession`` confirmed with ``confirm_current_contract`` and registries
rooted in a temporary directory.  No recognition output is fabricated here.
"""
from __future__ import annotations

from pathlib import Path

from services.grading_contract import AnswerSpec, finalize_answer_spec_for_category
from services.teaching_context import make_teaching_context
from services.template_session import TemplateSession


def paper_questions() -> list[dict]:
    return [
        {
            "question_no": "1",
            "question_text": "下列哪个是偶数？A.1 B.3 C.4 D.5",
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "C",
            "max_score": 5,
        },
        {
            "question_no": "2",
            "question_text": "多选：下列哪些是质数？A.2 B.4 C.5 D.9",
            "question_type": "choice",
            "choice_mode": "multiple",
            "standard_answer": "AC",
            "max_score": 5,
        },
        {
            "question_no": "3",
            "question_text": "计算 1/4 + 1/4",
            "question_type": "fill",
            "standard_answer": "1/2",
            "max_score": 5,
        },
        {
            "question_no": "4",
            "question_text": "解方程 2x+1=5，并说明理由",
            "question_type": "solve",
            "standard_answer": "x=2",
            "subquestion_count": 2,
            "subquestion_scores": [4, 6],
            "max_score": 10,
        },
    ]


def confirmed_detailed_template(
    upload_dir: str,
    template_id: str = "paper-src-1",
    *,
    teacher_id: int = 1,
    category: str = "exam",
    exam_name: str = "期中考试",
    questions: list[dict] | None = None,
    stage: str = "senior",
) -> TemplateSession:
    """A detailed-grading template confirmed through its real contract."""
    session = TemplateSession(
        template_id,
        upload_dir,
        teacher_id=teacher_id,
        teaching_context=make_teaching_context(stage),
    )
    session.category = category
    session.exam_name = exam_name if category == "exam" else ""
    finalized = [
        finalize_answer_spec_for_category(AnswerSpec.model_validate(item), category).model_dump()
        for item in (questions if questions is not None else paper_questions())
    ]
    session.pages = [TemplateSession.make_page(0, finalized)]
    session.status = "confirmed"
    session.confirm_current_contract(teacher_id)
    session.save_meta()
    return session


def teacher_confirmed_bubble_grid(
    question_no: str,
    region_bbox: list[int],
    options: str = "ABCD",
    *,
    subquestion_no: str = "",
) -> dict:
    """Teacher confirm request: equal option cells left to right in the box.

    Confirmation state, who and when are stamped by the server, never sent.
    """
    x1, y1, x2, y2 = region_bbox
    width = (x2 - x1) // len(options)
    return {
        "question_no": question_no,
        "subquestion_no": subquestion_no,
        "grid_order": "left_to_right",
        "cells": [
            {"option": option, "bbox": [x1 + i * width, y1, x1 + (i + 1) * width, y2]}
            for i, option in enumerate(options)
        ],
    }


def install_registries(monkeypatch, tmp_path: Path):
    """Point the router at isolated paper/answer-sheet registries."""
    from routers import answer_sheet
    from services.answer_sheet_paper_templates import PaperTemplateRegistry
    from services.answer_sheet_template_registry import AnswerSheetTemplateRegistry

    paper_registry = PaperTemplateRegistry(tmp_path / "paper-registry")
    sheet_registry = AnswerSheetTemplateRegistry(tmp_path / "sheet-registry")
    monkeypatch.setattr(answer_sheet, "get_paper_template_registry", lambda: paper_registry)
    monkeypatch.setattr(answer_sheet, "get_answer_sheet_template_registry", lambda: sheet_registry)
    return paper_registry, sheet_registry


def register_paper(client, template_id: str = "paper-src-1", alias: str = "") -> dict:
    response = client.post(
        "/api/answer-sheet/paper-templates",
        json={"template_id": template_id, "alias": alias},
    )
    assert response.status_code == 200, response.text
    return response.json()["template"]
