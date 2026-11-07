"""Answer-only recognition of student pages against a confirmed template.

A ``PaperCache`` is built once per batch from the teacher-confirmed template
(``PaperCache.from_confirmed_template_pages``) and is immutable afterwards.
For each student page it decides which template page the student page is
and transcribes only the student's answers; printed stems, question types
and answer slots always come from the template. When a page cannot be
matched or transcribed safely, the result fails closed for teacher review
instead of falling back to full-page OCR.

The code lives in one module per part; each imports only the parts above it:

    paper_cache_base            question numbers and the evidence strategies
    paper_cache_review          fail-closed results, transcription receipts, review placeholders
    paper_cache_prompts         the frozen page contract and the OCR prompts built from it (mixin)
    paper_cache_routing         which template page a student page is (mixin)
    paper_cache_answers         transcribing one page's answers (mixin)
    paper_cache_recovery        recovering a small OCR miss from its evidence crop (mixin)
    paper_cache_local_evidence  recognizing a page from locally built evidence (mixin)

This module keeps ``PaperCache`` itself (building it from the confirmed template
and ``recognize_page``; it inherits the six mixins) and re-exports every name, so
``from services.paper_cache import ...`` keeps working; new code may import from
the module that defines a name.
"""

# Everything this module imported before the split stays importable from it.
import asyncio
import copy
import hashlib
import json
import re
import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from services.accelerator_artifacts import (
    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
)
# Question numbers and evidence strategies (paper_cache_base.py).
from services.paper_cache_base import (  # noqa: F401
    _VISUAL_GRADING_EVIDENCE_STRATEGIES,
    _normalize_question_number,
    _question_number_sort_key,
    normalize_questions,
    question_number_signature,
)
# Fail-closed results, receipts and the review mixin (paper_cache_review.py).
from services.paper_cache_review import (  # noqa: F401
    _PaperCacheReviewMixin,
    _DIAGNOSTIC_ID,
    _RECEIPT_KEY,
    _RECEIPT_QUESTION_NUMBER,
    _ANSWER_STATES,
    _SLOT_CONTRACT_VIOLATED,
    _ZERO_READOUT,
    _ZERO_READOUT_MESSAGES,
    TranscriptionZeroReadout,
    _rejection_error_info,
    _fail_closed_ocr_result,
    _transcription_contract_failure,
    _transcription_receipt,
    _response_shape,
)
# OCR prompts and the prompt mixin (paper_cache_prompts.py).
from services.paper_cache_prompts import (  # noqa: F401
    _PaperCachePromptsMixin,
    RECOGNIZE_PAGE_AND_ANSWERS_PROMPT,
    RECOGNIZE_ANSWERS_ONLY_PROMPT,
    PAGE_IDENTITY_ROUTING_INSTRUCTION,
    RECOGNIZE_EVIDENCE_ONLY_PROMPT,
    RECOGNIZE_ANCHOR_CHUNKS_PROMPT,
    RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT,
    _compact_transcription_kinds,
)
# Page routing helpers and the routing mixin (paper_cache_routing.py).
from services.paper_cache_routing import (  # noqa: F401
    _PaperCacheRoutingMixin,
    _PRINTED_LABEL_EDGE_PUNCTUATION,
    _normalize_page_label_text,
    _MIN_STEM_SNIPPET_CHARS,
    _STEM_BIGRAM_CONTAINMENT,
    _content_text,
    _snippet_matches_stems,
)
# Answer constraints and the answer transcription mixin (paper_cache_answers.py).
from services.paper_cache_answers import (  # noqa: F401
    _PaperCacheAnswersMixin,
    _adjacent_answer_ambiguity,
    MAX_FILL_BLANK_LENGTH,
    _SPLIT_TRANSCRIPTION_EVIDENCE_STRATEGIES,
    is_fill_blank_question,
    _enforce_question_type_answer_constraints,
)
# The unreadable-answer recovery mixin (paper_cache_recovery.py).
from services.paper_cache_recovery import (  # noqa: F401
    _PaperCacheRecoveryMixin,
)
# The local evidence mixin (paper_cache_local_evidence.py).
from services.paper_cache_local_evidence import (  # noqa: F401
    _PaperCacheLocalEvidenceMixin,
)

logger = logging.getLogger(__name__)


class PaperCache(
    _PaperCacheReviewMixin,
    _PaperCachePromptsMixin,
    _PaperCacheRoutingMixin,
    _PaperCacheAnswersMixin,
    _PaperCacheRecoveryMixin,
    _PaperCacheLocalEvidenceMixin,
):
    """Frozen answer contract of one batch's template, keyed by page.

    ``_baselines`` maps each graded page label (``page_1``...) to
    ``{"ocr_result": {"questions": [...]}}``: the confirmed questions of that
    page. Build instances with ``from_confirmed_template_pages``.
    """

    def __init__(self):
        self._baselines: dict[str, dict] = {}
        # The joint page-match prompt is derived only from the confirmed
        # contract.  Cache its exact serialization so repeated student pages
        # send an identical reusable prefix to the vision scheduler.
        self._joint_prompt_contract_key = ""
        self._joint_prompt = ""
        self._template_evidence_matcher = None
        self._require_local_evidence = False
        # Teacher-chosen page roles and server-derived page identity of a
        # confirmed template.  Only explicitly chosen roles are recorded, so a
        # legacy template keeps its exact routing prompt.  Question-free
        # question_only/non_gradable pages are routable but never baselines.
        self._page_roles: dict[str, str] = {}
        self._page_identities: dict[str, dict] = {}
        self._non_graded_pages: dict[str, str] = {}
        self._accelerator_template_artifacts: dict = {}
        self._accelerator_template_dir = ""
        self._accelerator_template_id = ""

    @classmethod
    def from_confirmed_template_pages(
        cls,
        pages: list[dict],
        *,
        template_artifacts: dict | None = None,
        template_dir: str = "",
        require_local_evidence: bool = False,
        template_id: str = "",
    ) -> "PaperCache":
        """Build an immutable answer-only cache from teacher-confirmed pages."""
        if not pages:
            raise ValueError("confirmed template has no pages")
        from services.template_session import (
            GRADED_PAGE_ROLES,
            normalize_page_identity,
            page_role_of,
        )

        confirmed: dict[str, dict] = {}
        seen_labels: set[str] = set()
        seen_question_numbers: set[str] = set()
        page_roles: dict[str, str] = {}
        page_identities: dict[str, dict] = {}
        non_graded_pages: dict[str, str] = {}
        for position, page in enumerate(pages):
            try:
                page_number = int(page.get("page_index", position)) + 1
            except (TypeError, ValueError):
                page_number = position + 1
            label = f"page_{page_number}"
            if label in seen_labels:
                raise ValueError("confirmed template has duplicate page indexes")
            seen_labels.add(label)
            role = page_role_of(page)
            if "page_role" in page:
                page_roles[label] = role
            identity = normalize_page_identity(page.get("page_identity"))
            if identity:
                page_identities[label] = identity
            if role not in GRADED_PAGE_ROLES:
                # A question_only/non_gradable page is only a routing target:
                # a student page matched to it is reported as not graded.
                if page.get("questions"):
                    raise ValueError(f"confirmed template page {page_number} is not graded but has questions")
                non_graded_pages[label] = role
                continue

            from services.grading_contract import AnswerSpec

            questions = []
            for question in page.get("questions", []) or []:
                spec = AnswerSpec.model_validate(question)
                question_number = spec.question_no
                if not question_number:
                    continue
                if question_number in seen_question_numbers:
                    raise ValueError(f"confirmed template has duplicate question number: {question_number}")
                seen_question_numbers.add(question_number)
                frozen = spec.model_dump()
                frozen.pop("question_no", None)
                questions.append({"question_number": question_number, **frozen})
            if not questions:
                raise ValueError(f"confirmed template page {page_number} has no questions")
            confirmed[label] = {"ocr_result": {"questions": normalize_questions(questions)}}
        if not confirmed:
            raise ValueError("confirmed template has no graded pages")

        cache = cls()
        cache._baselines = confirmed
        cache._page_roles = page_roles
        cache._page_identities = page_identities
        cache._non_graded_pages = non_graded_pages
        if require_local_evidence and not template_artifacts:
            raise ValueError("local evidence is required but artifacts are missing")
        if template_artifacts:
            artifact_labels = set(template_artifacts)
            if artifact_labels != set(cache._baselines):
                raise ValueError("template artifact page types do not match confirmed pages")
            for label, artifact in template_artifacts.items():
                baseline_numbers = [
                    _normalize_question_number(str(question.get("question_number") or ""))
                    for question in cache._baselines[label]["ocr_result"].get("questions", [])
                ]
                artifact_numbers = [
                    _normalize_question_number(str(number))
                    for number in (
                        artifact.get("question_numbers")
                        or [
                            item.get("question_number")
                            for item in artifact.get("question_schema") or []
                        ]
                    )
                ]
                if baseline_numbers != artifact_numbers:
                    raise ValueError(
                        f"template artifact questions do not match {label}"
                    )
            from services.template_evidence import TemplateEvidenceMatcher
            cache._template_evidence_matcher = TemplateEvidenceMatcher(
                template_artifacts,
                template_dir,
            )
            cache._accelerator_template_artifacts = template_artifacts
            cache._accelerator_template_dir = template_dir
            cache._accelerator_template_id = str(template_id or "")
        cache._require_local_evidence = bool(require_local_evidence)
        return cache

    async def recognize_page(
        self,
        image_path: str,
        *,
        teacher_id: str = "",
        batch_id: str = "",
        paper_id: str = "",
    ) -> dict:
        """Match a student page to a template page and transcribe its answers.

        Local template evidence is used when the template has artifacts;
        otherwise one Vision request both routes the page and transcribes it.
        """
        if self._template_evidence_matcher is not None:
            result = await self._recognize_with_local_evidence(
                image_path,
                teacher_id=teacher_id,
                batch_id=batch_id,
                paper_id=paper_id,
            )
            zero_readout = self._zero_readout_failure(result)
            if zero_readout is not None:
                return zero_readout
            if result and not result.get("error"):
                logger.info(
                    "PaperCache: local evidence routed answers to '%s' - %d answers",
                    result.get("_cache_type_label"),
                    len(result.get("questions", [])),
                )
                return result
            if self._require_local_evidence:
                rejection_reason = result.get("error", "unknown") if result else "none"
                logger.warning(
                    "PaperCache: required local evidence rejected (%s); "
                    "all generative routing/full-page fallbacks are disabled",
                    rejection_reason,
                )
                accelerator_state = (
                    str(result.get("_accelerator_state") or "")
                    if isinstance(result, dict)
                    else ""
                )
                if accelerator_state in {"pending", "unavailable"}:
                    # These are scheduling/availability states, not evidence
                    # integrity failures. Keep the actionable accelerator
                    # message while retaining the fail-closed empty OCR shape.
                    return _fail_closed_ocr_result(
                        str(result.get("error") or "加速节点暂不可用"),
                        result,
                    )
                return _fail_closed_ocr_result(
                    "本次模板答案证据未通过校验，已停止自动批改该页",
                    result,
                )

        # The frozen contract is sent with the high-resolution transcription
        # request.  The model identifies the page from printed question-number
        # anchors and transcribes answers in the same response.
        result = await self._recognize_answers_only(
            image_path,
            teacher_id=teacher_id,
            auto_route=True,
        )
        zero_readout = self._zero_readout_failure(result)
        if zero_readout is not None:
            return zero_readout
        if result and not result.get("error"):
            logger.info(
                "PaperCache: safely routed answers-only OCR to '%s' — %d answers",
                result.get("_cache_type_label"), len(result.get("questions", [])),
            )
            return result
        rejection_reason = result.get("error", "unknown") if result else "none"

        # A teacher-confirmed template intentionally uses the answer-only path.
        # Falling back to full OCR would re-expose every printed stem to the same
        # generative model and recreate the answer-completion risk this route is
        # designed to avoid.  Fail closed and let the teacher inspect this page.
        logger.warning(
            "PaperCache: answers-only route rejected (%s); full OCR fallback is disabled",
            rejection_reason,
        )
        return _fail_closed_ocr_result(
            "答案视觉转录未通过校验，已停止自动批改该页",
            result,
        )
