"""Structured contracts for generated exercise papers."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


ExerciseQuestionType = Literal["choice", "fill", "solution"]
MAX_EXERCISE_QUESTIONS = 22
_TYPE_ALIASES = {
    "choice": "choice",
    "选择": "choice",
    "选择题": "choice",
    "fill": "fill",
    "blank": "fill",
    "填空": "fill",
    "填空题": "fill",
    "solution": "solution",
    "solve": "solution",
    "解答": "solution",
    "解答题": "solution",
    "大题": "solution",
}


def normalize_exercise_choice_answer(value: Any) -> str:
    """Normalize one or more A-D answers without accepting trailing prose."""
    text = str(value or "").strip().upper()
    text = re.sub(r"^(?:答案\s*[：:]\s*|选\s*)", "", text)
    text = re.sub(r"[\s,，、;/]+", "", text)
    text = text.rstrip(".。．")
    if not re.fullmatch(r"[A-D]{1,4}", text):
        return ""
    if len(set(text)) != len(text):
        return ""
    return "".join(label for label in "ABCD" if label in text)


class ExerciseTypeCounts(BaseModel):
    """Exact number of questions requested for each paper section."""

    model_config = ConfigDict(extra="forbid")

    choice: int = Field(4, ge=0, le=20)
    fill: int = Field(4, ge=0, le=20)
    solution: int = Field(2, ge=0, le=6)

    @model_validator(mode="after")
    def validate_total(self) -> "ExerciseTypeCounts":
        if not 1 <= self.total <= MAX_EXERCISE_QUESTIONS:
            raise ValueError(f"题目总数必须在 1 到 {MAX_EXERCISE_QUESTIONS} 之间")
        return self

    @property
    def total(self) -> int:
        return self.choice + self.fill + self.solution

    def as_dict(self) -> dict[ExerciseQuestionType, int]:
        return {
            "choice": self.choice,
            "fill": self.fill,
            "solution": self.solution,
        }

    @classmethod
    def from_total(cls, total: int) -> "ExerciseTypeCounts":
        """Backward-compatible 40/40/20 distribution for old count-only clients."""
        total = max(1, min(int(total), MAX_EXERCISE_QUESTIONS))
        if total == 1:
            return cls(choice=1, fill=0, solution=0)
        if total == 2:
            return cls(choice=1, fill=1, solution=0)
        solution = max(1, round(total * 0.2))
        remaining = total - solution
        choice = (remaining + 1) // 2
        fill = remaining - choice
        return cls(choice=choice, fill=fill, solution=solution)


class ExerciseQuestion(BaseModel):
    """One editable question in the generated paper."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    type: ExerciseQuestionType = Field(
        validation_alias=AliasChoices("type", "question_type")
    )
    question: str = Field(min_length=1, max_length=8000)
    options: dict[str, str] = Field(default_factory=dict)
    answer: str = Field(min_length=1, max_length=4000)
    solution: str = Field(min_length=1, max_length=12000)
    knowledge_point: str = Field(default="综合", max_length=200)
    difficulty: int = Field(default=2, ge=1, le=5)
    source_wrong_cycle_id: int | None = Field(default=None, ge=1)
    source_question_identity: str | None = Field(default=None, max_length=160)
    source_wrong_cycles: list[dict[str, int]] = Field(default_factory=list, max_length=200)
    wrong_student_count: int | None = Field(default=None, ge=1)
    wrong_event_count: int | None = Field(default=None, ge=1)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _TYPE_ALIASES.get(value.strip().lower(), value.strip().lower())
        return value

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options(cls, value: Any) -> dict[str, str]:
        if value in (None, ""):
            return {}
        if isinstance(value, list):
            value = {
                chr(ord("A") + index): item
                for index, item in enumerate(value[:4])
            }
        if not isinstance(value, dict):
            raise ValueError("options 必须是 A/B/C/D 字典")
        normalized: dict[str, str] = {}
        for key, item in value.items():
            label_match = re.search(r"[A-D]", str(key).upper())
            if not label_match:
                continue
            normalized[label_match.group(0)] = str(item).strip()
        return normalized

    @model_validator(mode="after")
    def validate_question_contract(self) -> "ExerciseQuestion":
        if self.type == "choice":
            if set(self.options) != {"A", "B", "C", "D"}:
                raise ValueError("选择题必须提供完整且唯一的 A/B/C/D 四个选项")
            if any(not self.options[label] for label in "ABCD"):
                raise ValueError("选择题选项不能为空")
            normalized_answer = normalize_exercise_choice_answer(self.answer)
            if not normalized_answer:
                raise ValueError("选择题答案必须是 1-4 个不重复的 A-D 字母")
            self.answer = normalized_answer
        elif self.options:
            raise ValueError("填空题和解答题不得携带 A/B/C/D 选项")
        return self


class ExercisePaper(BaseModel):
    """Structured paper payload shared by preview and DOCX export."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(default="高中数学练习", min_length=1, max_length=120)
    teaching_context: dict[str, str] = Field(default_factory=dict)
    questions: list[ExerciseQuestion] = Field(
        min_length=1,
        max_length=MAX_EXERCISE_QUESTIONS,
    )

    @field_validator("teaching_context")
    @classmethod
    def validate_teaching_context(cls, value: dict[str, str]) -> dict[str, str]:
        # An empty snapshot is an explicit legacy paper, never today's profile.
        if not value:
            return {}
        from services.teaching_context import validate_teaching_context

        return validate_teaching_context(value)

    @model_validator(mode="after")
    def stage_default_title(self) -> "ExercisePaper":
        if self.teaching_context and "title" not in self.model_fields_set:
            from services.teaching_context import stage_label

            self.title = f"{stage_label(self.teaching_context)}数学练习"
        return self
