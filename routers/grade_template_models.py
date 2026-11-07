"""What the template endpoints accept from a teacher: the request bodies, and why a submitted question is refused.

- ``TemplateConfirmReq`` (pages of ``_TemplateConfirmQuestion``) saves the
  checked template; ``TemplateCorrectionCandidatesReq`` asks which historical
  wrong questions it may correct.
- ``TemplateCutCandidatesReq`` asks for column layout candidates;
  ``TemplateLayoutOverridesReq`` saves one page's columns, question-number
  boxes and answer/handwriting regions; ``TemplateQuestionEditReq`` adds or
  removes one question number.
- ``_contract_rejection_reason`` turns a refused answer contract into the
  validator messages a teacher can act on.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class _TemplateConfirmQuestion(BaseModel):
    question_no: str
    question_text: str = ""
    question_type: str = "solve"
    choice_mode: str = "unknown"
    standard_answer: str = ""
    drawing_reference: dict | None = None
    answer_variants: list[str] = []
    solution_steps: list[str] = []
    options: list[str] | dict[str, str] = []
    figure_fingerprint: str = ""
    figure_description: str = ""
    max_score: float | None = None
    blank_count: int = 1
    answer_slots: list[str] = []
    fill_scoring_mode: str = "equal"
    blank_scores: list[float] = []
    subquestion_count: int = 1
    subquestion_scores: list[float] = []
    grading_method: str = ""
    chapter: str = ""
    topic: str = ""
    correction_excluded: bool = False
    # Kept for older clients. New saves authorize every server-discovered
    # candidate unless correction_excluded is true.
    authorized_wrong_cycle_ids: list[int] = []
    # Teacher decisions on draft Max extra/refused reports: slot IDs, or "*"
    # for a whole-question report, that the teacher chose to keep.
    max_flags_kept: list[str] = Field(default=[], max_length=40)


class _TemplateConfirmPage(BaseModel):
    page_index: int
    questions: list[_TemplateConfirmQuestion] = []


class TemplateConfirmReq(BaseModel):
    template_id: str
    category: str
    exam_name: str = ""
    pages: list[_TemplateConfirmPage] = []
    # Kept in the same confirmation payload for clients that submit the whole
    # draft at once.  Layout edits remain draft-only until this single save.
    column_mode: Literal["single", "double", "triple"] | None = None


class TemplateCorrectionCandidatesReq(BaseModel):
    template_id: str
    pages: list[_TemplateConfirmPage] = []


class TemplateCutCandidatesReq(BaseModel):
    action_id: str
    cut_types: list[Literal["question"]] = ["question"]
    image_type: Literal["scan", "photo"] = "photo"
    # Normal browser retries must reuse the frozen template plan.  A new
    # layout-model request is allowed only when the teacher explicitly forces it.
    force: bool = False


class TemplateColumnOverride(BaseModel):
    column_index: int
    x1_1000: int
    x2_1000: int


class TemplateQuestionAnchorOverride(BaseModel):
    question_number: str
    # New model results carry the question identity only.  Keep this optional
    # so already-saved templates can still be opened and migrated.
    question_number_bbox: list[int] | None = None
    # Optional client fields are accepted for forward compatibility, but are
    # overwritten by the server before geometry is persisted.
    confidence: float | None = None
    human_confirmed: bool | None = None
    confidence_source: str | None = None

    @field_validator("question_number")
    @classmethod
    def validate_question_number(cls, value: str) -> str:
        value = value.strip() if isinstance(value, str) else value
        if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
            raise ValueError("question_number must start and end with digits; dots cannot be consecutive")
        return value


class TemplateHandwritingRegionOverride(BaseModel):
    question_number: str
    regions: list[list[int]]
    # Client-supplied provenance is never trusted as teacher confirmation.
    confidence: float | None = None
    human_confirmed: bool | None = None
    confidence_source: str | None = None

    @field_validator("question_number")
    @classmethod
    def validate_question_number(cls, value: str) -> str:
        value = value.strip() if isinstance(value, str) else value
        if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
            raise ValueError("question_number must start and end with digits; dots cannot be consecutive")
        return value


class TemplateAnswerRegionOverride(BaseModel):
    question_number: str
    regions: list[list[int]]
    slot_ids: list[str] | None = None
    region_kinds: list[str] | None = None
    confidence: float | None = None
    human_confirmed: bool | None = None
    confidence_source: str | None = None

    @field_validator("question_number")
    @classmethod
    def validate_question_number(cls, value: str) -> str:
        value = value.strip() if isinstance(value, str) else value
        if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
            raise ValueError("question_number must start and end with digits; dots cannot be consecutive")
        return value


class TemplateLayoutOverridesReq(BaseModel):
    page_index: int
    columns: list[TemplateColumnOverride]
    question_anchors: list[TemplateQuestionAnchorOverride]
    template_answer_regions: list[TemplateAnswerRegionOverride] | None = None
    template_handwriting_regions: list[TemplateHandwritingRegionOverride] | None = None
    # Only drawing geometry may change through this endpoint; directory
    # identity and printed paths are always copied from the server snapshot.
    question_schema: list[dict] | None = None


class TemplateQuestionEditReq(BaseModel):
    """Add or remove one question number on a page.

    ``before_question_number`` names the question the new one is placed in front
    of; empty appends to the end of the page. The resulting number is derived
    from that position by the service layer, so a request cannot put a number
    and its renumbering out of step. ``new_question_number`` lets the teacher
    type the number for the new question; it is validated (digits and dots
    only, unique) and applied without renumbering the rest.
    """

    page_index: int
    action: str
    question_number: str = ""
    before_question_number: str = ""
    new_question_number: str = ""

    @field_validator("question_number", "before_question_number", "new_question_number")
    @classmethod
    def validate_optional_question_number(cls, value: str) -> str:
        value = value.strip() if isinstance(value, str) else value
        if value == "":
            return ""
        if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
            raise ValueError("question number must start and end with digits; dots cannot be consecutive")
        return value


def _contract_rejection_reason(exc: ValueError) -> str:
    """Teacher-readable reason: validator messages only, never the row dump."""
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        messages = [str(item.get("msg") or "").removeprefix("Value error, ") for item in exc.errors()]
        return "；".join(message for message in messages if message) or "题目合同无效"
    return str(exc)
