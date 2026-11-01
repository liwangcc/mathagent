"""Canonical teacher-confirmed grading contract shared by every input path."""

from __future__ import annotations

import json
import copy
import math
import re
import unicodedata
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


_VALID_QUESTION_TYPES = {"choice", "fill", "solve"}
_VALID_CHOICE_MODES = {"single", "multiple", "unknown"}
_VALID_GRADING_METHODS = {"exact", "equivalent", "process"}
_VALID_FILL_SCORING_MODES = {"equal", "all_or_nothing", "custom"}
VALID_GRADING_CATEGORIES = frozenset({"daily", "exam"})
_CHOICE_ANSWER_RE = re.compile(
    r"^(?:答案\s*[：:]\s*|选\s*)?([A-D](?:\s*[,，、;/；|+\s]?\s*[A-D]){0,3})(?:[.。．])?$",
    re.IGNORECASE,
)


def normalize_choice_answer(value: Any) -> str:
    """Return canonical unique A-D options in option order, or an empty string.

    Harmless wrappers and separators are accepted. Invalid characters and
    duplicate option letters fail closed instead of becoming trusted answers.
    """
    match = _CHOICE_ANSWER_RE.fullmatch(unicodedata.normalize("NFKC", str(value or "")).strip())
    if not match:
        return ""
    letters = re.findall(r"[A-D]", match.group(1).upper())
    if not letters or len(letters) != len(set(letters)):
        return ""
    return "".join(sorted(letters))


def normalize_choice_mode(value: Any, *, question_type: str = "choice") -> str:
    """Normalize the explicit single/multiple contract without guessing it."""
    if question_type != "choice":
        return "unknown"
    normalized = str(value or "unknown").strip().lower()
    return normalized if normalized in _VALID_CHOICE_MODES else "unknown"


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = [raw]
            except (TypeError, ValueError):
                value = raw.splitlines()
        else:
            value = raw.splitlines()
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
    return cleaned


def merge_directory_leaf_score_layer(row: dict) -> dict:
    """Fold a stale subquestion score layer into the root total of a directory leaf.

    Frozen contract: a directory leaf scores by answer regions; a subquestion
    score layer must merge into the root total and never survive as a second
    scoring hierarchy (末级题按作答区域计分，小问分值合成根节点总分). Both
    half-flattened legacy rows (subquestion_count=1 with a stale [5, 0]) and
    fresh editor echoes (the frontend slot editor mirrors the legacy
    "subquestions = slots" model with subquestion_count = slot count) fold
    here; AnswerSpec itself stays strict and rejects any submitted layer.
    """
    if not isinstance(row, dict) or not row.get("directory_node_id"):
        return row
    scores = row.get("subquestion_scores")
    score_list = list(scores) if isinstance(scores, (list, tuple)) else []
    layer_count = row.get("subquestion_count") not in (None, 1, "1")
    if not layer_count and len(score_list) <= 1:
        return row
    numbers: list[float] = []
    for score in score_list:
        try:
            number = float(score)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            numbers.append(number)
    total = sum(numbers)
    merged = dict(row)
    merged["subquestion_count"] = 1
    question_type = str(row.get("question_type") or "").strip().lower()
    if question_type == "solve":
        # A solve leaf's region-aligned score list IS its scoring channel
        # (作答区域计分); keep the teacher's numbers verbatim. Only an
        # unaligned legacy layer folds into the single root score.
        regions = int(row.get("blank_count") or 0) or len(row.get("slot_ids") or []) or 1
        if len(score_list) > 1 and len(score_list) != regions:
            merged["subquestion_scores"] = [total] if total > 0 else []
        # else: verbatim (the submitted region scores stay untouched)
    else:
        # fill/choice: grading reads blank_scores — the subquestion mirror is
        # editor noise and never overrides the teacher's per-region totals.
        merged["subquestion_scores"] = []
        if merged.get("max_score") in (None, ""):
            blank_total = 0.0
            for value in merged.get("blank_scores") or []:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number > 0:
                    blank_total += number
            if blank_total > 0:
                merged["max_score"] = blank_total
            elif total > 0:
                merged["max_score"] = total
    return merged


def reconcile_drawing_contract(row: dict) -> dict:
    """Reconcile a row's drawing contract to the teacher's explicit marks.

    region_kinds are the per-region marks and the single source of truth (the
    frontend derives answer_kind from them in updateSlotStructure, and R4
    demands the same derivation server-side): a stale model answer_kind
    re-derives from the marks, and a resolved non-graphic row's leftover
    drawing geometry (regions / label anchor) is deleted model noise that
    clears. Stale model layers must never block a teacher correction
    (incident a5d95c2a: "修正后的结果因为跟模型的错误层级不匹配而被阻断").
    Rows with neither mark stay untouched — mark-less ambiguous producer data
    keeps failing closed upstream.
    """
    if not isinstance(row, dict):
        return row
    kinds = row.get("region_kinds")
    reconciled = row
    if isinstance(kinds, (list, tuple)) and kinds:
        expected = (
            ("mixed" if "answer" in kinds else "drawing")
            if "drawing" in kinds else "non_graphic"
        )
        if reconciled.get("answer_kind") != expected:
            reconciled = dict(reconciled)
            reconciled["answer_kind"] = expected
    if str(reconciled.get("answer_kind") or "").strip().lower() == "non_graphic":
        if reconciled.get("drawing_regions") or reconciled.get("label_anchor") is not None:
            reconciled = dict(reconciled)
            reconciled["drawing_regions"] = []
            reconciled["label_anchor"] = None
    return reconciled


class AnswerSpec(BaseModel):
    """The only shape allowed to cross the teacher-confirmation boundary."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    question_no: str = Field(
        validation_alias=AliasChoices("question_no", "question_number"),
    )
    question_text: str = Field(
        default="",
        validation_alias=AliasChoices("question_text", "content", "question"),
    )
    question_type: str = "solve"
    choice_mode: str = "unknown"
    standard_answer: str = Field(
        default="",
        validation_alias=AliasChoices("standard_answer", "answer"),
    )
    answer_variants: list[str] = Field(default_factory=list)
    solution_steps: list[str] = Field(default_factory=list)
    solve_visual_context: dict | None = None
    # Scoring belongs to the confirmed batch/template, never to the permanent
    # question-bank identity.  Daily homework ignores point values but still
    # uses blank/subquestion structure for item-level correctness analytics.
    max_score: float | None = None
    blank_count: int = 1
    answer_slots: list[str] = Field(default_factory=list)
    # Optional server-owned physical-slot types. Absent on historical contracts.
    region_kinds: list[str] = Field(default_factory=list)
    slot_ids: list[str] | None = None
    fill_scoring_mode: str = "equal"
    blank_scores: list[float] = Field(default_factory=list)
    subquestion_count: int = 1
    subquestion_scores: list[float] = Field(default_factory=list)
    grading_method: str = ""
    chapter: str = ""
    topic: str = ""
    # Teacher decision for correction discovery. False means saving the
    # template authorizes every currently discovered history candidate.
    correction_excluded: bool = False
    matched: bool = True
    question_bank_id: int | None = None
    difficulty: int = 3
    source: str = "teacher_confirmed"
    # The directory parent is NOT the scoring engine's parent_question_no.
    # Physical answer slots may share a directory leaf.
    directory_node_id: str = ""
    directory_parent_id: str | None = None
    printed_path: list[str | None] = Field(default_factory=list)
    answer_kind: str = "non_graphic"
    drawing_regions: list[list[float]] = Field(default_factory=list)
    label_anchor: list[float] | None = None
    # Editable answer content, deliberately separate from directory ownership.
    drawing_reference: dict | None = None
    # The hierarchy model's own per-page number before the paper-wide merge
    # step renumbered it. Provenance only; absent on single-number contracts.
    page_local_number: str | None = None
    # Server-owned locator hint: the page prints no bracket/underline for this
    # single text slot, so a student may answer before the number or at the
    # end of the stem. Absent on historical and printed-position contracts.
    answer_position: str | None = None
    # Draft-only Max extra/refused reports. Confirmation requires the teacher
    # to delete or explicitly keep each one; they never enter a frozen answer.
    max_flags: list[dict] = Field(default_factory=list)

    def directory_metadata(self) -> dict:
        fields = {}
        if self.page_local_number is not None:
            fields["page_local_number"] = self.page_local_number
        if self.answer_position is not None:
            fields["answer_position"] = self.answer_position
        if self.slot_ids is not None:
            fields["slot_ids"] = list(self.slot_ids)
        if self.region_kinds:
            fields["region_kinds"] = list(self.region_kinds)
        if self.directory_node_id:
            fields.update(directory_node_id=self.directory_node_id,
                          directory_parent_id=self.directory_parent_id,
                          printed_path=list(self.printed_path))
        if self.directory_node_id or self.answer_kind != "non_graphic":
            fields.update(answer_kind=self.answer_kind,
                          drawing_regions=[list(box) for box in self.drawing_regions],
                          label_anchor=list(self.label_anchor) if self.label_anchor else None)
        return fields

    def model_dump(self, **kwargs) -> dict:
        result = super().model_dump(**kwargs)
        # Old frozen schemas must not acquire new default keys when reopened.
        allowed = self.directory_metadata()
        for key in ("directory_node_id", "directory_parent_id", "printed_path",
                    "answer_kind", "drawing_regions", "label_anchor"):
            if key not in allowed:
                result.pop(key, None)
        if self.drawing_reference is None:
            result.pop("drawing_reference", None)
        if self.solve_visual_context is None:
            result.pop("solve_visual_context", None)
        if not self.region_kinds:
            result.pop("region_kinds", None)
        if self.slot_ids is None:
            result.pop("slot_ids", None)
        if self.page_local_number is None:
            result.pop("page_local_number", None)
        if self.answer_position is None:
            result.pop("answer_position", None)
        if not self.max_flags:
            result.pop("max_flags", None)
        return result

    @field_validator("max_flags", mode="before")
    @classmethod
    def _normalize_max_flags(cls, value: Any) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 40:
            raise ValueError("max_flags must be a bounded list")
        flags = []
        for flag in value:
            if (not isinstance(flag, dict) or flag.get("status") not in {"extra", "refused"}
                    or flag.get("source") not in {"locator", "solver"}
                    or flag.get("scope") not in {"question", "slot"}
                    or (flag["scope"] == "slot") != isinstance(flag.get("slot_id"), str)
                    or not isinstance(flag.get("reason", ""), str)):
                raise ValueError("invalid max review flag")
            flags.append({"scope": flag["scope"],
                          **({"slot_id": flag["slot_id"][:32]} if flag["scope"] == "slot" else {}),
                          "status": flag["status"], "reason": flag.get("reason", "")[:240],
                          "source": flag["source"]})
        return flags

    @field_validator("answer_position", mode="before")
    @classmethod
    def _normalize_answer_position(cls, value: Any) -> str | None:
        # A hint, not a contract: anything but the one known value means the
        # page prints its answer position, which is today's behaviour.
        return "unmarked" if value == "unmarked" else None

    @field_validator("drawing_reference", mode="before")
    @classmethod
    def _normalize_drawing_reference(cls, value: Any) -> dict | None:
        if value is None:
            return None
        from services.drawing_reference import normalize_reference, validate_confirmed_reference
        # This model is also used for reading frozen storage. The router owns
        # authorization; provider candidate binding always clears this flag.
        confirmed = isinstance(value, dict) and value.get("teacher_confirmed") is True
        if confirmed and "reference_hash" in value:
            return validate_confirmed_reference(value)
        return normalize_reference(value, trusted_confirmed=confirmed)

    @field_validator("question_no", "question_text", "standard_answer", "chapter", "topic", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("answer_variants", "solution_steps", mode="before")
    @classmethod
    def _normalize_lists(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("answer_slots", mode="before")
    @classmethod
    def _normalize_answer_slots(cls, value: Any) -> list[str]:
        # Slots are positions, not a set of alternative answers. Repeated and
        # empty values must survive draft saving without moving later answers.
        if value is None:
            return []
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    value = value.splitlines()
            else:
                value = value.splitlines()
        if not isinstance(value, (list, tuple)):
            raise ValueError("answer_slots must be an ordered list")
        return ["" if item is None else str(item).strip() for item in value]

    @field_validator("max_score", mode="before")
    @classmethod
    def _normalize_max_score(cls, value: Any) -> float | None:
        if value is None or str(value).strip() == "":
            return None
        score = float(value)
        if not math.isfinite(score) or score < 0 or score > 1000:
            raise ValueError("max_score must be a finite number between 0 and 1000")
        return score

    @field_validator("blank_count", "subquestion_count", mode="before")
    @classmethod
    def _normalize_count(cls, value: Any) -> int:
        count = int(value)
        if count < 1 or count > 20:
            raise ValueError("count must be between 1 and 20")
        return count

    @field_validator("subquestion_scores", "blank_scores", mode="before")
    @classmethod
    def _normalize_subquestion_scores(cls, value: Any) -> list[float]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            try:
                parsed = json.loads(raw)
                value = parsed if isinstance(parsed, list) else [parsed]
            except (TypeError, ValueError):
                value = [part.strip() for part in raw.replace("，", ",").split(",")]
        if not isinstance(value, (list, tuple)):
            value = [value]
        scores: list[float] = []
        for item in value:
            score = float(item)
            if not math.isfinite(score) or score < 0 or score > 1000:
                raise ValueError("subquestion score must be a finite number between 0 and 1000")
            scores.append(score)
        if len(scores) > 20:
            raise ValueError("subquestion_scores cannot contain more than 20 items")
        return scores

    @field_validator("fill_scoring_mode", mode="before")
    @classmethod
    def _normalize_fill_scoring_mode(cls, value: Any) -> str:
        normalized = str(value or "equal").strip().lower()
        return normalized if normalized in _VALID_FILL_SCORING_MODES else "equal"

    @field_validator("question_type", mode="before")
    @classmethod
    def _normalize_question_type(cls, value: Any) -> str:
        normalized = str(value or "solve").strip().lower()
        return normalized if normalized in _VALID_QUESTION_TYPES else "solve"

    @field_validator("grading_method", mode="before")
    @classmethod
    def _normalize_method_text(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _VALID_GRADING_METHODS else ""

    @field_validator("choice_mode", mode="before")
    @classmethod
    def _normalize_choice_mode(cls, value: Any) -> str:
        normalized = str(value or "unknown").strip().lower()
        return normalized if normalized in _VALID_CHOICE_MODES else "unknown"

    @model_validator(mode="after")
    def _default_grading_method(self) -> "AnswerSpec":
        if self.slot_ids is not None:
            from services.template_slots import normalize_slot_ids
            self.slot_ids = normalize_slot_ids(self.question_no, self.slot_ids, self.blank_count)
            if len(self.answer_slots) != len(self.slot_ids):
                raise ValueError("slot_ids must align with answer_slots")
        if self.answer_kind not in {"non_graphic", "drawing", "mixed"}:
            raise ValueError("invalid answer_kind")
        if self.region_kinds:
            if (len(self.region_kinds) != self.blank_count
                    or any(kind not in {"answer", "drawing"} for kind in self.region_kinds)):
                raise ValueError("作答区域类型数量或类型无效")
            expected = ("mixed" if "answer" in self.region_kinds else "drawing") if "drawing" in self.region_kinds else "non_graphic"
            if self.answer_kind != expected:
                raise ValueError("作答区域类型与作图合同不一致")
        # Type describes the printed/scoring structure; answer_kind describes
        # its response medium. A fill or choice item can also require figure
        # work, without losing its original type, choice mode or score policy.
        if self.drawing_reference is not None and self.answer_kind not in {"drawing", "mixed"}:
            raise ValueError("标准图只能用于作图题或图文混合题；请删除该题标准图，或把作图属性改回作图题")
        if self.directory_node_id:
            if self.subquestion_count != 1:
                raise ValueError("目录末级题按作答区域计分，不能另建小问层级")
            if len(self.subquestion_scores) > 1:
                # A solve leaf scores by its answer regions: an exactly
                # region-aligned score list IS that channel (scoring_engine
                # aligns it to the answer boxes). Anything else — including any
                # layer on a fill/choice leaf — is a subquestion hierarchy
                # re-expansion attempt and stays rejected.
                aligned_solve_regions = (
                    self.question_type == "solve"
                    and len(self.subquestion_scores) == self.blank_count
                )
                if not aligned_solve_regions:
                    raise ValueError("目录末级题按作答区域计分，不能另建小问层级")
        self.choice_mode = normalize_choice_mode(
            self.choice_mode,
            question_type=self.question_type,
        )
        if not self.grading_method:
            self.grading_method = {
                "choice": "exact",
                "fill": "equivalent",
                # No process requirement was specified. Preserve explicit
                # confirmed values, but do not invent one from question type.
                "solve": "",
            }[self.question_type]
        return self

    def to_answer_map_entry(self, *, source: str | None = None) -> dict:
        return {
            **self.directory_metadata(),
            **({"drawing_reference": copy.deepcopy(self.drawing_reference)} if self.drawing_reference is not None else {}),
            "matched": bool(self.matched),
            "standard_answer": self.standard_answer,
            "answer_variants": list(self.answer_variants),
            "solution_steps": list(self.solution_steps),
            "max_score": self.max_score,
            "blank_count": self.blank_count,
            "answer_slots": list(self.answer_slots),
            "fill_scoring_mode": self.fill_scoring_mode,
            "blank_scores": list(self.blank_scores),
            "subquestion_count": self.subquestion_count,
            "subquestion_scores": list(self.subquestion_scores),
            "grading_method": self.grading_method,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "choice_mode": self.choice_mode,
            "question_bank_id": self.question_bank_id,
            "chapter": self.chapter,
            "topic": self.topic,
            "difficulty": self.difficulty,
            "source": source or self.source,
        }


def normalize_answer_spec(data: dict | AnswerSpec) -> AnswerSpec:
    return data if isinstance(data, AnswerSpec) else AnswerSpec.model_validate(data)


def finalize_answer_spec_for_category(data: dict | AnswerSpec, category: str) -> AnswerSpec:
    """Return the frozen batch contract with safe category-specific defaults."""
    category = str(category or "daily").strip().lower()
    if category not in VALID_GRADING_CATEGORIES:
        raise ValueError("批改类型只能是 daily 或 exam")
    spec = normalize_answer_spec(data).model_copy(deep=True)

    if spec.question_type == "choice" and spec.answer_kind != "drawing":
        normalized_choice_answer = normalize_choice_answer(spec.standard_answer)
        if not normalized_choice_answer:
            raise ValueError("选择题标准答案必须是 1–4 个不重复的 A、B、C、D 选项；请核对题型或答案")
        if spec.choice_mode == "unknown":
            raise ValueError("选择题必须由教师确认是单选题还是多选题")
        if spec.choice_mode == "single" and len(normalized_choice_answer) != 1:
            raise ValueError("单选题标准答案必须恰好包含一个选项")
        if spec.choice_mode == "multiple" and len(normalized_choice_answer) < 2:
            raise ValueError("多选题标准答案必须至少包含两个选项")
        spec.standard_answer = normalized_choice_answer
        if spec.slot_ids is not None and spec.answer_kind == "non_graphic":
            if len(spec.slot_ids) != 1:
                raise ValueError("每道独立选择题必须单独编号并只有一个答案槽")
            if normalize_choice_answer(spec.answer_slots[0]) != normalized_choice_answer:
                raise ValueError("选择题标准答案与答案槽不一致，请重新核对保存")

    if spec.region_kinds:
        if "drawing" in spec.region_kinds and (spec.fill_scoring_mode != "equal" or spec.blank_scores):
            raise ValueError("图文混合题按整题图文结果计分，不支持逐空分值或全有全无模式")
        if len(spec.answer_slots) != len(spec.region_kinds):
            raise ValueError("作答区域类型与答案位数量必须一致")
        for kind, answer in zip(spec.region_kinds, spec.answer_slots):
            if kind == "drawing" and answer:
                raise ValueError("作图答案位必须保留为空，文字要求写在整题标准答案中")
            if kind == "answer" and not answer:
                raise ValueError("文字答案位不得为空")
    elif spec.question_type == "fill" and spec.blank_count > 1 and spec.answer_kind != "drawing":
        if not spec.answer_slots:
            parts = [
                item.strip() for item in spec.standard_answer.replace("；", ";").split(";")
                if item.strip()
            ]
            if len(parts) == spec.blank_count:
                spec.answer_slots = parts
        if len(spec.answer_slots) != spec.blank_count or any(not slot for slot in spec.answer_slots):
            raise ValueError("多空填空题的答案位数量必须与空数一致")

    if category != "exam":
        # Structure remains available for daily correctness analytics, but point
        # values are not part of the daily result contract.
        spec.max_score = None
        spec.fill_scoring_mode = "equal"
        spec.blank_scores = []
        return spec

    if spec.question_type in {"choice", "fill"}:
        if spec.question_type == "fill" and spec.blank_count > 1 and spec.fill_scoring_mode == "custom":
            if len(spec.blank_scores) != spec.blank_count:
                raise ValueError("自定义多空分值数量必须与空数一致")
            if any(score <= 0 for score in spec.blank_scores):
                raise ValueError("自定义多空分值必须全部大于 0")
            custom_total = round(sum(spec.blank_scores), 4)
            if spec.max_score is None:
                spec.max_score = custom_total
            elif not math.isclose(spec.max_score, custom_total, rel_tol=0, abs_tol=0.0001):
                raise ValueError("自定义多空分值之和必须等于本题总分")
        else:
            if spec.max_score is None:
                spec.max_score = 5.0
            if spec.question_type != "fill" or spec.blank_count <= 1:
                spec.fill_scoring_mode = "equal"
                spec.blank_scores = []
            elif spec.fill_scoring_mode != "custom":
                spec.blank_scores = []
        if spec.max_score is None or spec.max_score <= 0:
            raise ValueError("考试中的选择题和填空题分值必须大于 0")
    elif spec.question_type == "solve":
        if not spec.subquestion_scores:
            count = min(max(spec.subquestion_count, 1), 20)
            defaults = [5.0, 7.0, 8.0]
            spec.subquestion_scores = [
                defaults[index] if index < len(defaults) else defaults[-1]
                for index in range(count)
            ]
        if not any(score > 0 for score in spec.subquestion_scores):
            raise ValueError("考试解答题至少要有一个分值大于 0 的小问")
        spec.subquestion_count = sum(1 for score in spec.subquestion_scores if score > 0)
        spec.max_score = None
    return spec
