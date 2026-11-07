"""Answer-only recognition of student pages against a confirmed template.

A ``PaperCache`` is built once per batch from the teacher-confirmed template
(``PaperCache.from_confirmed_template_pages``) and is immutable afterwards.
For each student page it decides which template page the student page is
and transcribes only the student's answers; printed stems, question types
and answer slots always come from the template. When a page cannot be
matched or transcribed safely, the result fails closed for teacher review
instead of falling back to full-page OCR.
"""

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
from services.paper_cache_base import (
    _VISUAL_GRADING_EVIDENCE_STRATEGIES,
    _normalize_question_number,
    _question_number_sort_key,
    normalize_questions,
    question_number_signature,
)
from services.paper_cache_review import (
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
from services.paper_cache_prompts import (
    _PaperCachePromptsMixin,
    RECOGNIZE_PAGE_AND_ANSWERS_PROMPT,
    RECOGNIZE_ANSWERS_ONLY_PROMPT,
    PAGE_IDENTITY_ROUTING_INSTRUCTION,
    RECOGNIZE_EVIDENCE_ONLY_PROMPT,
    RECOGNIZE_ANCHOR_CHUNKS_PROMPT,
    RECOGNIZE_FULL_HEIGHT_TRANSCRIPTION_PROMPT,
    _compact_transcription_kinds,
)

logger = logging.getLogger(__name__)


def _adjacent_answer_ambiguity(question_answers: list[dict]) -> set[str]:
    """Fail closed when one visual answer is plausibly split across two rows.

    Full-column difference images deliberately omit printed question text.  A
    fraction/line break immediately before a blank neighbour is therefore a
    known high-risk shape: the vision model may attach the neighbour's
    denominator to the preceding question (for example ``Q22=4/1, Q23=blank``).
    Do not guess which row owns the ink; send both rows to teacher review.
    """
    answers = [item for item in question_answers if isinstance(item, dict)]
    ambiguous: set[str] = set()
    for index, current in enumerate(answers[:-1]):
        following = answers[index + 1]
        current_no = _normalize_question_number(str(current.get("question_number") or ""))
        following_no = _normalize_question_number(str(following.get("question_number") or ""))
        if not current_no or not following_no:
            continue
        current_text = str(current.get("student_answer") or "").strip()
        following_text = str(following.get("student_answer") or "").strip()
        following_state = str(following.get("answer_state") or "").strip().lower()
        fraction_like = (
            "\\frac" in current_text
            or "/" in current_text
            or ("\n" in current_text and any(ch.isdigit() for ch in current_text))
        )
        if fraction_like and following_state == "blank" and not following_text:
            ambiguous.update({current_no, following_no})
    return ambiguous


# ── 填空题答案长度检查 ──
MAX_FILL_BLANK_LENGTH = 45            # 填空题答案最大字符数（超过直接判错）
_SPLIT_TRANSCRIPTION_EVIDENCE_STRATEGIES = (
    _VISUAL_GRADING_EVIDENCE_STRATEGIES
    | {"confirmed_bbox_focus_sheets"}
)


# ── 冻结模板后的页型匹配与学生答案转录 prompt ──


# ═══════════════════════════════════════════════════════
# 题号归一化
# ═══════════════════════════════════════════════════════


_PRINTED_LABEL_EDGE_PUNCTUATION = " .．。、,，:：;；()（）[]【】"


def _normalize_page_label_text(value: object) -> str:
    """Compare printed page/question labels without spacing or edge punctuation.

    ``"1."``, ``"（1）"`` and ``" 1 "`` read as the same printed label; case is
    folded for Latin captions.  An empty result means "not observed".
    """
    text = "".join(str(value or "").split())
    return text.strip(_PRINTED_LABEL_EDGE_PUNCTUATION).casefold()


# A printed-stem opening shorter than this is too generic ("计算", "解方程")
# to tell two exams apart.
_MIN_STEM_SNIPPET_CHARS = 6
_STEM_BIGRAM_CONTAINMENT = 0.7


def _content_text(value: object) -> str:
    """Printed text reduced to letters/digits/CJK for OCR-tolerant comparison."""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def _snippet_matches_stems(snippet: str, stems: list[str]) -> bool:
    """Whether a normalized stem opening seen on a student page is one of these stems."""
    if any(snippet in stem for stem in stems):
        return True
    grams = {snippet[index:index + 2] for index in range(len(snippet) - 1)}
    if not grams:
        return False
    for stem in stems:
        stem_grams = {stem[index:index + 2] for index in range(len(stem) - 1)}
        if len(grams & stem_grams) / len(grams) >= _STEM_BIGRAM_CONTAINMENT:
            return True
    return False


def is_fill_blank_question(question_text: str) -> bool:
    """判断是否是填空题。

    填空题特征：题目中有括号、下划线、空白处等，
    且没有"求"、"证明"、"计算"等解答题关键词。
    """
    fill_keywords = ['（', '(', '_', '____', '填空', '填入']
    solve_keywords = ['求', '证明', '计算', '解', '化简', '推导']

    has_fill = any(kw in question_text for kw in fill_keywords)
    has_solve = any(kw in question_text for kw in solve_keywords)

    return has_fill and not has_solve


def _enforce_question_type_answer_constraints(questions: list[dict]) -> None:
    """Apply the frozen answer grammar after OCR and before grading.

    A handwritten choice letter can be emitted as a visually similar digit
    (the observed ``B`` -> ``13`` confusion). Once the template has already
    confirmed that the location is a choice question, this one explicit
    confusion is normalized before grading while the OCR value is retained
    for audit. Other values remain fail-closed and go to review.
    """
    from services.grading_contract import normalize_choice_answer

    for question in questions:
        if not isinstance(question, dict):
            continue
        if str(question.get("question_type") or "").strip().lower() != "choice":
            continue
        if str(question.get("answer_state") or "").strip().lower() != "visible":
            continue
        raw_answer = str(question.get("student_answer") or "").strip()
        if raw_answer == "13":
            question["_ocr_raw_student_answer"] = raw_answer
            question["student_answer"] = "B"
            question["_ocr_normalization_reason"] = "choice_ocr_confusion_13_to_B"
            continue
        option = normalize_choice_answer(raw_answer)
        bracketed = re.fullmatch(r"[（(]?\s*([A-Da-d]{1,4})\s*[)）]?", raw_answer)
        if not option and bracketed:
            # Exemption rectangles can preserve one printed parenthesis next
            # to a clearly transcribed option. Never infer letters from text.
            option = normalize_choice_answer(bracketed.group(1))
        choice_mode = str(question.get("choice_mode") or "unknown").strip().lower()
        valid_for_mode = bool(
            option
            and (
                (choice_mode == "single" and len(option) == 1)
                or (choice_mode == "multiple" and 1 <= len(option) <= 4)
            )
        )
        if valid_for_mode:
            if bracketed and bracketed.group(1) != raw_answer:
                question["_ocr_raw_student_answer"] = raw_answer
                question["_ocr_normalization_reason"] = "choice_enclosing_parentheses"
            question["student_answer"] = option
            continue
        question["_ocr_raw_student_answer"] = raw_answer
        question["student_answer"] = "[无法识别]"
        question["answer_state"] = "unreadable"


class PaperCache(_PaperCacheReviewMixin, _PaperCachePromptsMixin):
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


    def _drawing_template_context(self, page_type_id: str):
        """Load only the hash-bound canonical template page, never a whiteout image.

        It is the pixel frame of the rectified student canvas and of the
        teacher's normalized standard figure. A blank page is the difference
        baseline; a marked page only tells the gate what is already printed.
        """
        artifact = self._accelerator_template_artifacts.get(page_type_id) or {}
        mode = "blank" if artifact.get("template_mode", "marked") == "blank" else "marked"
        try:
            import io
            import numpy as np
            from PIL import Image
            from services.template_evidence import _safe_artifact_path

            path, _relative = _safe_artifact_path(
                self._accelerator_template_dir, artifact.get("template_image", "")
            )
            path = Path(path)
            if path.stat().st_size > 64_000_000:
                raise ValueError("template too large")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
                raise ValueError("template hash mismatch")
            with Image.open(io.BytesIO(raw)) as image:
                if (image.size != (artifact.get("width"), artifact.get("height"))
                        or image.width * image.height > 40_000_000):
                    raise ValueError("template dimensions mismatch")
                return mode, np.array(image.convert("RGB"))
        except (OSError, ValueError, TypeError):
            # Missing baseline is unavailable evidence, never a pixel score of zero.
            logger.warning("PaperCache: drawing template baseline unavailable")
            return mode, None

    async def _recognize_with_local_evidence(
        self,
        image_path: str,
        teacher_id: str = "",
        batch_id: str = "",
        paper_id: str = "",
    ) -> dict:
        """Build trusted answer evidence, using a node first and CPU only on a
        durable unavailable/failed handoff.

        A live remote job remains authoritative while it is pending.  When the
        accelerator is definitively unavailable, ``claim_cpu_fallback`` provides
        the cross-worker compare-and-swap that allows exactly one server worker
        to run the verified matcher.  The matcher always consumes immutable
        template artifacts and its output is explicitly marked server-owned.
        """
        from services.ocr_service import _get_local_ocr_preprocess_semaphore
        from services.template_evidence import TemplateEvidenceError, cleanup_evidence

        matcher = self._template_evidence_matcher

        def _accelerator_failure(
            state: str,
            reason_code: str,
            message: str | None = None,
        ) -> dict:
            public_state = state if state in {"pending", "unavailable"} else "fault"
            messages = {
                "pending": "等待教师客户端加速节点完成图片处理",
                "unavailable": "加速节点不可用，请使用客户端",
                "fault": "客户端加速图片处理发生故障，已暂停自动批改该页",
            }
            return {
                "error": message or messages[public_state],
                "_accelerator_state": public_state,
                "_accelerator_reason_code": reason_code,
            }

        evidence = None
        evidence_paths: tuple[str, ...] = ()
        evidence_authority = ""
        accelerator_handle = None
        local_evidence = False
        local_temporary = None
        cpu_fallback_claimed = False
        preprocessing_succeeded = False
        # Set once the evidence names a confirmed page. From then on a failed
        # page keeps review placeholders, so a multi-page template cannot
        # strand the whole batch behind one unroutable failure.
        routed_page_type = ""
        try:
            has_accelerator_context = all((
                teacher_id,
                batch_id,
                paper_id,
                self._accelerator_template_id,
                self._accelerator_template_dir,
                self._accelerator_template_artifacts,
            ))
            if has_accelerator_context:
                from services.accelerator_business import (
                    AcceleratorEvidence,
                    CLIENT_EVIDENCE_CONTRACT_VERSION,
                    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION,
                    claim_cpu_fallback,
                    try_accelerated_evidence,
                    cpu_fallback_has_active_owner,
                )

                accelerator_handle = await try_accelerated_evidence(
                    teacher_id=teacher_id,
                    batch_id=batch_id,
                    paper_id=paper_id,
                    source_path=image_path,
                    template_id=self._accelerator_template_id,
                    artifacts=self._accelerator_template_artifacts,
                    template_dir=self._accelerator_template_dir,
                )
                raw_state = str(getattr(accelerator_handle, "state", "") or "")
                reason_code = str(
                    getattr(accelerator_handle, "reason_code", "")
                    or "accelerator_state_invalid"
                )
                if raw_state == "pending":
                    # A queued/leased node may still produce authoritative
                    # evidence.  Never run a second matcher while it can.
                    accelerator_handle = None
                    return _accelerator_failure("pending", reason_code)
                if raw_state in {"unavailable", "failed"}:
                    job_ids = tuple(
                        str(job_id).strip()
                        for job_id in (getattr(accelerator_handle, "job_ids", ()) or ())
                        if str(job_id).strip()
                    )
                    if job_ids:
                        # ``claim_cpu_fallback`` intentionally accepts only an
                        # explicit cpu_fallback state.  Do not let an ordinary
                        # unavailable/pending observation mutate job rows.
                        fallback_request = AcceleratorEvidence(
                            None,
                            job_ids,
                            state="cpu_fallback",
                            reason_code=reason_code,
                        )
                        claimed = await asyncio.to_thread(
                            claim_cpu_fallback,
                            fallback_request,
                        )
                        if claimed is None:
                            # A refused claim is not itself proof of a live
                            # owner. Re-read durable state, never the stale
                            # unavailable/failed observation. This request owns
                            # nothing, including in finally's cleanup path.
                            accelerator_handle = None
                            active_owner = await asyncio.to_thread(
                                cpu_fallback_has_active_owner,
                                fallback_request,
                                teacher_id=teacher_id,
                                batch_id=batch_id,
                                paper_id=paper_id,
                                source_path=image_path,
                                template_id=self._accelerator_template_id,
                            )
                            return _accelerator_failure(
                                "pending" if active_owner else (
                                    "unavailable" if raw_state == "unavailable" else "fault"
                                ),
                                "accelerator_fallback_claim_lost",
                                "图片处理仍在进行，请等待当前处理者完成" if active_owner else
                                "加速任务正在由其他处理者接管，已停止重复配准该页",
                            )
                        evidence = getattr(claimed, "evidence", None)
                        accelerator_handle = claimed if claimed.job_ids else None
                        cpu_fallback_claimed = bool(
                            claimed.job_ids
                            and not evidence
                            and str(getattr(claimed, "state", "")) == "cpu_fallback"
                        )
                    else:
                        # No durable remote work exists, so the verified local
                        # matcher can safely handle this page directly.
                        accelerator_handle = None
                elif raw_state == "ready":
                    evidence = getattr(accelerator_handle, "evidence", None)
                    if evidence is None:
                        return _accelerator_failure(
                            "fault",
                            "accelerator_evidence_missing",
                        )
                else:
                    accelerator_handle = None
                    return _accelerator_failure("fault", reason_code)

            # No node, an empty enqueue, or an atomically claimed CPU fallback
            # reaches this branch.  Keep native image work bounded per worker.
            if evidence is None:
                if matcher is None:
                    return _accelerator_failure(
                        "fault",
                        "server_template_evidence_unavailable",
                    )
                try:
                    batch_root = Path(image_path).resolve().parent
                    local_temporary = tempfile.TemporaryDirectory(
                        dir=str(batch_root),
                        prefix=".template-evidence-",
                    )
                    async with _get_local_ocr_preprocess_semaphore():
                        evidence = await asyncio.to_thread(
                            matcher.match_and_build,
                            image_path,
                            local_temporary.name,
                            include_evidence=True,
                        )
                    local_evidence = True
                except (TemplateEvidenceError, OSError, ValueError) as exc:
                    logger.warning(
                        "PaperCache: local template evidence rejected %s (%s)",
                        image_path,
                        exc,
                    )
                    return _accelerator_failure(
                        "fault",
                        "server_template_evidence_failed",
                        f"本次模板本地配准失败，已停止自动批改该页: {exc}",
                    )

            evidence_paths = tuple(
                str(path).strip()
                for path in (getattr(evidence, "evidence_paths", ()) or ())
                if str(path).strip()
            )
            legacy_evidence_path = str(getattr(evidence, "evidence_path", "") or "").strip()
            if not evidence_paths and legacy_evidence_path:
                evidence_paths = (legacy_evidence_path,)
            if evidence is None or not evidence_paths:
                return _accelerator_failure(
                    "fault",
                    "accelerator_evidence_missing",
                )
            try:
                if any(
                    not Path(path).is_file() or Path(path).stat().st_size <= 0
                    for path in evidence_paths
                ):
                    return _accelerator_failure(
                        "fault",
                        "accelerator_evidence_missing",
                    )
            except OSError:
                return _accelerator_failure(
                    "fault",
                    "accelerator_evidence_missing",
                )

            if local_evidence:
                # The matcher is server-owned.  Make that boundary explicit so
                # downstream code cannot mistake CPU output for client pixels.
                local_diagnostics = dict(getattr(evidence, "diagnostics", {}) or {})
                ownership = str(
                    local_diagnostics.get("ownership")
                    or "full_column_difference"
                )
                local_diagnostics["accelerator"] = {
                    "server_validated": True,
                    "candidate_reused_for_acceptance": False,
                    "evidence_authority": (
                        "server_rendered_full_columns"
                        if ownership == "full_column_difference"
                        else "server_cpu_replay"
                    ),
                    "server_source_transform_replayed": True,
                    "server_evidence_rendered": True,
                    "executor": "business_cpu_template_evidence",
                }
                evidence = replace(evidence, diagnostics=local_diagnostics)

            diagnostics = getattr(evidence, "diagnostics", None)
            if not isinstance(diagnostics, dict):
                return _accelerator_failure(
                    "fault",
                    "accelerator_evidence_contract_invalid",
                )
            accelerator_diagnostics = diagnostics.get("accelerator")
            evidence_authority = (
                accelerator_diagnostics.get("evidence_authority")
                if isinstance(accelerator_diagnostics, dict)
                else ""
            )
            if not isinstance(accelerator_diagnostics, dict) or (
                evidence_authority not in {
                    "trusted_client_evidence_multi",
                    "trusted_client_evidence_focus_sheets",
                    "trusted_client_evidence_full_columns",
                    "bound_client_full_columns",
                    "server_rendered_full_columns",
                    "server_cpu_replay",
                    "trusted_client_evidence",
                    "bound_client_artifact",
                }
                or (
                    evidence_authority == "bound_client_full_columns"
                    and (
                        accelerator_diagnostics.get("bound_node_identity_validated")
                        is not True
                        or accelerator_diagnostics.get("local_quality_report_validated")
                        is not True
                        or diagnostics.get("answer_source_complete") is not True
                    )
                )
                or (
                    evidence_authority in {
                        "server_rendered_full_columns",
                        "server_cpu_replay",
                    }
                    and (
                        accelerator_diagnostics.get("server_source_transform_replayed")
                        is not True
                        or accelerator_diagnostics.get("server_evidence_rendered")
                        is not True
                    )
                )
                or (
                    evidence_authority not in {
                        "server_rendered_full_columns",
                        "server_cpu_replay",
                    }
                    and accelerator_diagnostics.get("server_source_transform_replayed")
                    is not False
                )
            ):
                return _accelerator_failure(
                    "fault",
                    "accelerator_evidence_contract_invalid",
                )

            page_type_id = str(getattr(evidence, "page_type_id", "") or "").strip()
            if page_type_id not in self._baselines:
                return _accelerator_failure(
                    "fault",
                    "accelerator_evidence_page_type_invalid",
                )
            routed_page_type = page_type_id

            if local_evidence and diagnostics.get("answer_source_complete") is not True:
                return self._with_review_placeholders(
                    _accelerator_failure(
                        "fault",
                        "server_template_evidence_incomplete",
                    ),
                    routed_page_type,
                )

            if local_evidence:
                preprocessing_succeeded = True

            # Ink both before the number and after the stem of an unmarked
            # slot has no trustworthy owner side: teacher review, not a guess.
            incomplete = {
                _normalize_question_number(str(item))
                for item in [*diagnostics.get("incomplete_answer_regions", []),
                             *(diagnostics.get("answer_position_review") or [])]
                if _normalize_question_number(str(item))
            }
            strategy = str(
                diagnostics.get("ownership")
                or (
                    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
                    if evidence_authority in {
                        "trusted_client_evidence_full_columns",
                        "bound_client_full_columns",
                        "server_rendered_full_columns",
                    }
                    else
                    CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION
                    if evidence_authority == "trusted_client_evidence_multi"
                    else CLIENT_EVIDENCE_CONTRACT_VERSION
                )
            )
            # Both remote and local evidence enter one answers-only Vision
            # request.  The source image itself is never sent after matching.
            from services.drawing_grading import build_text_evidence
            drawing_questions = self._baselines[page_type_id]["ocr_result"].get("questions", [])
            with tempfile.TemporaryDirectory(prefix=".text-evidence-", dir=Path(image_path).resolve().parent) as text_dir:
                text_paths = await asyncio.to_thread(
                    build_text_evidence, evidence_paths, diagnostics, drawing_questions, text_dir,
                )
                result = await self._recognize_answers_only(
                    text_paths[0],
                    page_type_id,
                    teacher_id=teacher_id,
                    evidence_mode=True,
                    evidence_strategy=strategy,
                    evidence_paths=text_paths,
                    evidence_diagnostics=diagnostics,
                    program_blank_qns=set(getattr(evidence, "blank_question_numbers", []) or []),
                    program_unreadable_qns=incomplete,
                )
            if not result.get("error"):
                result["_local_template_evidence"] = not local_evidence
                if local_evidence:
                    result["_server_template_evidence"] = True
                result["_full_page_transcription"] = False
                result["_ocr_source"] = (
                    CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
                    if evidence_authority in {
                        "trusted_client_evidence_full_columns",
                        "bound_client_full_columns",
                        "server_rendered_full_columns",
                    }
                    else (
                        CLIENT_EVIDENCE_MULTI_CONTRACT_VERSION
                        if evidence_authority in {
                            "trusted_client_evidence_multi",
                            "trusted_client_evidence_focus_sheets",
                        }
                        else CLIENT_EVIDENCE_CONTRACT_VERSION
                    )
                )
                result["_template_match_diagnostics"] = diagnostics
                # Graphic evidence is a separate derived input. Do not modify
                # the frozen transcription image or retain temporary paths.
                from services.drawing_grading import assess_page
                drawing_mode, drawing_template = "marked", None
                if any(q.get("answer_kind") in {"drawing", "mixed"} for q in drawing_questions):
                    drawing_mode, drawing_template = await asyncio.to_thread(
                        self._drawing_template_context, page_type_id
                    )
                drawing_assessments = await assess_page(
                    source_path=image_path, page_type_id=page_type_id,
                    questions=drawing_questions,
                    transcriptions=result.get("questions", []),
                    evidence_paths=evidence_paths, diagnostics=diagnostics,
                    teacher_id=teacher_id, template_mode=drawing_mode,
                    blank_template=drawing_template if drawing_mode == "blank" else None,
                    marked_template=drawing_template if drawing_mode == "marked" else None,
                )
                if drawing_assessments:
                    result["_drawing_assessments"] = drawing_assessments
            if result.get("error"):
                result = self._with_review_placeholders(result, routed_page_type)
            return result
        except Exception as exc:
            from services.processing_errors import report_failure
            info = report_failure(exc, stage="image", task_id=batch_id)
            result = _accelerator_failure(
                "fault",
                "accelerator_evidence_processing_failed",
                info["message"],
            )
            result["error_info"] = info
            return self._with_review_placeholders(result, routed_page_type)
        finally:
            if local_temporary is not None:
                try:
                    await asyncio.to_thread(local_temporary.cleanup)
                except OSError as exc:
                    logger.warning("Failed to clean local template evidence: %s", exc)
            if evidence is not None and not local_evidence and evidence_authority not in {
                "trusted_client_evidence_multi",
                "trusted_client_evidence_focus_sheets",
                "trusted_client_evidence_full_columns",
                "bound_client_full_columns",
                "server_rendered_full_columns",
            }:
                try:
                    for path in evidence_paths:
                        await asyncio.to_thread(cleanup_evidence, path)
                except (OSError, ValueError) as exc:
                    logger.warning("Failed to clean client evidence image: %s", exc)
            if accelerator_handle is not None and accelerator_handle.job_ids:
                from services.accelerator_business import finish_business_preprocessing

                try:
                    await asyncio.to_thread(
                        finish_business_preprocessing,
                        accelerator_handle,
                        fallback_succeeded=(
                            preprocessing_succeeded if cpu_fallback_claimed else False
                        ),
                        error_code=(
                            "" if preprocessing_succeeded
                            else "business_preprocessing_failed"
                        ),
                    )
                except Exception as exc:
                    logger.warning("Failed to close accelerator business job: %s", exc)

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

    async def _recognize_answers_only(
        self,
        image_path: str,
        type_label: str | None = None,
        teacher_id: str = "",
        *,
        evidence_mode: bool = False,
        evidence_strategy: str = "",
        evidence_paths: tuple[str, ...] = (),
        evidence_diagnostics: dict | None = None,
        program_blank_qns: set[str] | None = None,
        program_unreadable_qns: set[str] | None = None,
        alignment_verification: int | bool = False,
        auto_route: bool = False,
    ) -> dict:
        """Transcribe answers and preserve a separate grading handoff when enabled."""
        from services.ocr_service import (
            _prepare_vision_image,
            _cleanup_prepared_image,
            call_vision,
            call_vision_images,
            ocr_failure_result,
            validate_visual_transcriptions,
        )
        from services.key_pool import get_ocr_teacher_semaphore
        from services.analyzer import parse_llm_json

        from config import OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS

        split_transcription_required = bool(
            evidence_mode
            and evidence_strategy in _SPLIT_TRANSCRIPTION_EVIDENCE_STRATEGIES
        )
        auto_route = bool(auto_route and not evidence_mode)
        prompt = (
            self._build_evidence_only_prompt(
                type_label,
                evidence_strategy=evidence_strategy,
                split_transcription=True,
            )
            if evidence_mode
            else (
                self._build_page_and_answers_prompt()
                if auto_route
                else self._build_answer_only_prompt(
                    type_label,
                    alignment_verification=alignment_verification,
                )
            )
        )
        if not prompt:
            logger.warning("PaperCache: cannot build prompt for '%s'", type_label)
            return {"error": "无法构建答案识别 prompt"}

        from services.template_slots import slot_transcription_prompt, build_slot_evidence_prompt, bind_student_slots
        prompt_labels = list(self._baselines) if auto_route else [str(type_label or "")]
        for slot_label in prompt_labels:
            slot_questions = self._baselines.get(slot_label, {}).get("ocr_result", {}).get("questions", [])
            if evidence_mode and evidence_strategy in _VISUAL_GRADING_EVIDENCE_STRATEGIES:
                prompt = build_slot_evidence_prompt(prompt, slot_questions)
            else:
                prompt += slot_transcription_prompt(slot_questions)

        # Handwritten answers and short math strokes need more pixels than the
        # page-routing pass.  Keep the same bounded worker lifecycle while using
        # the dedicated answer-transcription resolution.
        from config import OCR_ANSWER_PREPROCESS_MAX_EDGE, OCR_PER_TEACHER_LIMIT
        processed_path = image_path
        if not evidence_mode:
            processed_path = await _prepare_vision_image(
                image_path,
                max_edge=OCR_ANSWER_PREPROCESS_MAX_EDGE,
            )

        # 答案-only 路径直接调 call_vision，故在此显式套每老师并发限制，
        # 与 recognize_paper 口径一致（否则基准建立后的并发批次会绕过上限）。
        teacher_sem = await get_ocr_teacher_semaphore(teacher_id, OCR_PER_TEACHER_LIMIT) if teacher_id else None

        request_receipt: dict = {}
        request_kwargs = {
            "max_tokens": OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS,
            "request_receipt": request_receipt,
        }
        if evidence_mode:
            # The validated full-column run used one deterministic request:
            # temperature=0, thinking disabled, streaming enabled, and no
            # second model attempt that could silently change ownership.
            request_kwargs["max_attempts"] = 1
        if auto_route:
            # Put the stable frozen contract before the per-student image so
            # compatible Responses providers can reuse the shared prefix.
            request_kwargs["prompt_first"] = True
        student_fingerprint = ""
        if auto_route and any(
            identity.get("visual_fingerprint")
            for identity in self._page_identities.values()
        ):
            from services.template_page_identity import compute_visual_fingerprint
            try:
                student_fingerprint = await asyncio.to_thread(
                    compute_visual_fingerprint, image_path,
                )
            except (OSError, ValueError) as exc:
                # A missing fingerprint only removes one supporting signal.
                logger.warning("PaperCache: student page fingerprint unavailable: %s", exc)
        if auto_route:
            def routed_response_validator(content: str) -> None:
                candidate = parse_llm_json(content)
                if not isinstance(candidate, dict):
                    raise ValueError("联合页型识别结果必须是 JSON 对象")
                selected, _observed = self._resolve_joint_page_type(
                    candidate, visual_fingerprint=student_fingerprint,
                )
                if selected in self._non_graded_pages:
                    return
                from services.template_slots import text_slot_ids
                baseline_questions = self._baselines[selected]["ocr_result"].get("questions", [])
                routed_slots = {
                    str(q.get("question_number") or ""): text_slot_ids(str(q.get("question_number") or ""), q)
                    for q in baseline_questions
                    if q.get("slot_ids") is not None
                    and text_slot_ids(str(q.get("question_number") or ""), q)
                }
                drawing_only_numbers = {
                    str(q.get("question_number") or "")
                    for q in baseline_questions
                    if str(q.get("answer_kind") or "").strip().lower() == "drawing"
                }
                candidate_questions = validate_visual_transcriptions(
                    candidate.get("questions", []),
                    slot_contracts=routed_slots,
                    drawing_only_numbers=drawing_only_numbers,
                )
                expected = self._expected_question_numbers(selected)
                actual = [
                    _normalize_question_number(
                        str(item.get("question_number") or "")
                    )
                    for item in candidate_questions
                ]
                if actual != expected:
                    raise ValueError("联合识别答案题号或顺序与页型合同不一致")

            request_kwargs["response_validator"] = routed_response_validator
        local_evidence_conflict_qns: set[str] = set()
        selected_label = str(type_label or "").strip()
        observed_question_numbers: list[str] = []
        sent_paths = [str(path) for path in evidence_paths] if evidence_paths else [processed_path]
        raw_result = None
        data = None
        receipt = None
        try:
            if teacher_sem:
                async with teacher_sem:
                    raw_result = await (
                        call_vision_images(
                            list(evidence_paths),
                            prompt,
                            **request_kwargs,
                        )
                        if evidence_paths
                        else call_vision(
                            processed_path,
                            prompt,
                            **request_kwargs,
                        )
                    )
            else:
                raw_result = await (
                    call_vision_images(
                        list(evidence_paths),
                        prompt,
                        **request_kwargs,
                    )
                    if evidence_paths
                    else call_vision(
                        processed_path,
                        prompt,
                        **request_kwargs,
                    )
                )
            data = parse_llm_json(raw_result)
            receipt = await asyncio.to_thread(
                _transcription_receipt, sent_paths, raw_result, data, request_receipt,
            )
            if not isinstance(data, dict):
                raise ValueError("答案识别结果必须是 JSON 对象")
            if auto_route:
                selected_label, observed_question_numbers = self._resolve_joint_page_type(
                    data, visual_fingerprint=student_fingerprint,
                )
                if selected_label in self._non_graded_pages:
                    return self._not_graded_page_result(selected_label)
            slot_contracts = {
                str(q.get("question_number") or ""): q
                for q in self._baselines[selected_label]["ocr_result"].get("questions", [])
                if q.get("slot_ids") is not None
            }
            raw_answers = data.get("questions", [])
            if not isinstance(raw_answers, list):
                raise ValueError("questions must be a list")
            raw_answers = [
                bind_student_slots(a, str(a.get("question_number") or ""), slot_contracts[str(a.get("question_number") or "")])
                if isinstance(a, dict) and str(a.get("question_number") or "") in slot_contracts else a
                for a in raw_answers
            ]
            # Recorded before any program-evidence rebinding below, so this
            # names only questions whose model response broke the contract.
            slot_contract_violations = sorted(
                str(a.get("question_number") or "")
                for a in raw_answers
                if isinstance(a, dict)
                and str(a.get("question_number") or "") in slot_contracts
                and a.get("slot_identity_error")
            )
            drawing_only_numbers = {
                number for number, contract in slot_contracts.items()
                if str(contract.get("answer_kind") or "").strip().lower() == "drawing"
            }
            student_answers = validate_visual_transcriptions(
                raw_answers,
                drawing_only_numbers=drawing_only_numbers,
            )
            # Multi-box all-blank claims are scored as blank (unanswered -> wrong).
            # Product ruling 2026-09-30 ("unclear handwriting is judged wrong"):
            # faint strokes the model reads as blank no longer go to teacher
            # review; explicit [无法识别] slots keep their unreadable/needs-review
            # routing untouched.
            # A fraction-like answer followed by a blank adjacent row is not a
            # trustworthy transcription in a difference-only full-column view.
            # Preserve the raw model response for audit, but make the derived
            # answers fail closed so grading cannot silently join two rows.
            adjacent_ambiguity_qns = _adjacent_answer_ambiguity(student_answers)
            if adjacent_ambiguity_qns:
                for answer in student_answers:
                    question_no = _normalize_question_number(
                        str(answer.get("question_number") or "")
                    )
                    if question_no in adjacent_ambiguity_qns:
                        answer["student_answer"] = "[无法识别]"
                        answer["answer_state"] = "unreadable"
                        local_evidence_conflict_qns.add(question_no)
            program_blank_candidates = {
                _normalize_question_number(str(qn))
                for qn in (program_blank_qns or set())
                if _normalize_question_number(str(qn))
            }
            if program_blank_candidates:
                for answer in student_answers:
                    normalized_question_no = _normalize_question_number(
                        str(answer.get("question_number") or "")
                    )
                    if normalized_question_no in program_blank_candidates:
                        # Local blank detection is conservative but imperfect:
                        # faint or shifted handwriting can survive visually even
                        # when the pixel heuristic reports no ink.  Only accept a
                        # blank when both detectors agree; conflicts need review.
                        if answer.get("answer_state") != "blank":
                            local_evidence_conflict_qns.add(normalized_question_no)
                            answer["student_answer"] = "[无法识别]"
                            answer["answer_state"] = "unreadable"
            program_unreadable = {
                _normalize_question_number(str(qn))
                for qn in (program_unreadable_qns or set())
                if _normalize_question_number(str(qn))
            }
            if program_unreadable:
                for answer in student_answers:
                    normalized_question_no = _normalize_question_number(
                        str(answer.get("question_number") or "")
                    )
                    if normalized_question_no in program_unreadable:
                        # Fail closed only for answer boxes whose authoritative
                        # source pixels are incomplete; crop context may extend
                        # beyond the photographed page after alignment.
                        if answer.get("answer_state") != "unreadable":
                            local_evidence_conflict_qns.add(normalized_question_no)
                            answer["student_answer"] = "[无法识别]"
                            answer["answer_state"] = "unreadable"
            # Evidence OCR is transcription-only. Any unreadable recovery here
            # still produces transcription text for the separate grading
            # provider; it never creates or accepts a visual judgment payload.
            if (
                evidence_mode
                and not slot_contracts
                and evidence_strategy not in _VISUAL_GRADING_EVIDENCE_STRATEGIES
                and evidence_paths
                and evidence_diagnostics
            ):
                if teacher_sem:
                    async with teacher_sem:
                        recovered = await self._recover_unreadable_evidence_answers(
                            type_label,
                            student_answers,
                            evidence_paths,
                            evidence_diagnostics,
                            blocked_question_numbers=(
                                program_unreadable | program_blank_candidates | adjacent_ambiguity_qns
                            ),
                        )
                else:
                    recovered = await self._recover_unreadable_evidence_answers(
                        type_label,
                        student_answers,
                        evidence_paths,
                        evidence_diagnostics,
                        blocked_question_numbers=(
                            program_unreadable | program_blank_candidates | adjacent_ambiguity_qns
                        ),
                    )
                if recovered:
                    for answer in student_answers:
                        question_number = _normalize_question_number(
                            str(answer.get("question_number") or "")
                        )
                        replacement = recovered.get(question_number)
                        if replacement is not None:
                            answer.update(replacement)
            for answer in student_answers:
                number = str(answer.get("question_number") or "")
                if number in slot_contracts and number in local_evidence_conflict_qns:
                    answer.pop("student_slots", None)
                    answer.update(bind_student_slots(answer, number, slot_contracts[number]))
        except Exception as e:
            failure = ocr_failure_result(e, message=f"答案识别失败: {e}")
            if receipt is None:
                receipt = await asyncio.to_thread(
                    _transcription_receipt, sent_paths, raw_result, data, request_receipt,
                )
            failure["_ocr_receipt"] = receipt
            return failure
        finally:
            if not evidence_mode:
                _cleanup_prepared_image(image_path, processed_path)

        if selected_label not in self._baselines:
            return _transcription_contract_failure(
                "答案转录缺少已确认页型，需教师复核", receipt,
            )
        baseline = self._baselines[selected_label]
        baseline_questions = baseline["ocr_result"].get("questions", [])
        expected_qns = [
            _normalize_question_number(str(q.get("question_number", "")))
            for q in baseline_questions
            if _normalize_question_number(str(q.get("question_number", "")))
        ]

        answer_qns = [
            _normalize_question_number(str(answer.get("question_number", "")))
            for answer in student_answers
        ]
        if answer_qns != expected_qns:
            return _transcription_contract_failure(
                "答案题号或顺序与已确认页型不一致，需教师复核", receipt,
            )

        # 构建答案映射
        answer_map = {}
        for a in student_answers:
            raw_qn = str(a.get("question_number", ""))
            norm_qn = _normalize_question_number(raw_qn)
            answer_map[norm_qn] = {
                "student_answer": a.get("student_answer", ""),
                "answer_state": a.get("answer_state", ""),
                "_ocr_local_recovery": bool(a.get("_ocr_local_recovery")),
                **{key: a[key] for key in ("student_slots", "slot_identity_error") if key in a},
            }

        # 合并：题干来自基准，答案来自本次识别
        merged_questions = []
        for q in baseline_questions:
            qn = q.get("question_number", "")
            norm_qn = _normalize_question_number(qn)
            answer = answer_map.get(norm_qn, {
                "student_answer": "", "answer_state": "blank",
            })
            merged_questions.append({
                "question_number": qn,
                "question_text": q.get("question_text", ""),
                "question_type": q.get("question_type", "solve") or "solve",
                "choice_mode": q.get("choice_mode", "unknown") or "unknown",
                "student_answer": answer["student_answer"],
                "answer_state": answer["answer_state"],
                "_ocr_local_recovery": answer.get("_ocr_local_recovery", False),
                **{key: answer[key] for key in ("student_slots", "slot_identity_error") if key in answer},
                **{key: q[key] for key in ("slot_ids", "region_kinds") if key in q},
            })

        _enforce_question_type_answer_constraints(merged_questions)

        # 填空题答案长度检查
        for mq in merged_questions:
            question_text = mq.get("question_text", "")
            student_answer = mq.get("student_answer", "")
            if is_fill_blank_question(question_text):
                if len(student_answer.strip()) > MAX_FILL_BLANK_LENGTH:
                    mq["result"] = "wrong"
                    mq["explanation"] = "填空题答案过长，可能是演算过程"
                    mq["needs_review"] = False
                    logger.info(
                        "PaperCache: fill-blank answer too long (%d > %d chars) for Q%s, marked wrong",
                        len(student_answer.strip()), MAX_FILL_BLANK_LENGTH,
                        mq.get("question_number", "?"),
                    )

        result = {
            "questions": merged_questions,
            "student_name": "",
            "student_no": "",
            "_cache_type_label": selected_label,
            "_answers_only": True,  # 标记是短输出模式
            "_local_template_evidence": evidence_mode,
        }
        if adjacent_ambiguity_qns:
            result["_answer_ambiguity_qns"] = sorted(adjacent_ambiguity_qns)
            result["_answer_ambiguity_reason"] = (
                "相邻题答案疑似被整列差分图合并，已转人工复核"
            )
        if split_transcription_required:
            result["_transcription_only"] = True
            result["_requires_separate_grading"] = True
        if auto_route:
            result["_observed_question_numbers"] = observed_question_numbers
        if slot_contract_violations:
            result["_slot_contract_violations"] = slot_contract_violations
        result["_ocr_receipt"] = receipt
        return result

    async def _recover_unreadable_evidence_answers(
        self,
        type_label: str | None,
        student_answers: list[dict],
        evidence_paths: tuple[str, ...],
        evidence_diagnostics: dict,
        *,
        blocked_question_numbers: set[str] | None = None,
    ) -> dict[str, dict]:
        """Recover a small OCR miss from its existing continuous crop.

        The second request is deliberately gated by a visible answer in the same
        crop.  It is a transcription-only request and cannot replace a whole
        page result when the crop or its source pixels are incomplete.
        """
        if evidence_diagnostics.get("answer_source_complete") is not True:
            return {}
        # Full-column evidence deliberately has no per-question answer boxes;
        # do not route an unreadable result through the legacy bbox recovery.
        if evidence_diagnostics.get("ownership") == "full_column_difference":
            return {}
        focus_mode = (
            evidence_diagnostics.get("ownership")
            == "confirmed_bbox_focus_sheets"
        )
        blocks = evidence_diagnostics.get("evidence_blocks")
        focus_locations = evidence_diagnostics.get("evidence_locations")
        if focus_mode:
            if not isinstance(focus_locations, list) or not focus_locations:
                return {}
        elif not isinstance(blocks, list) or not blocks:
            return {}
        if type_label not in self._baselines:
            return {}

        artifact = self._accelerator_template_artifacts.get(type_label)
        if not isinstance(artifact, dict):
            return {}
        regions = artifact.get("answer_regions")
        if not isinstance(regions, list):
            return {}
        crop_plan = artifact.get("question_crop_plan")
        if not focus_mode and (
            not isinstance(crop_plan, dict)
            or not isinstance(crop_plan.get("blocks"), list)
        ):
            return {}

        baseline_questions = self._baselines[type_label]["ocr_result"].get(
            "questions", []
        )
        expected_qns = [
            _normalize_question_number(str(question.get("question_number") or ""))
            for question in baseline_questions
            if _normalize_question_number(str(question.get("question_number") or ""))
        ]
        focus_location_by_qn = {}
        if focus_mode:
            for location in focus_locations:
                if not isinstance(location, dict):
                    return {}
                question_number = _normalize_question_number(
                    str(location.get("question_number") or "")
                )
                image_index = location.get("image_index")
                bbox = location.get("answer_bbox_1000")
                if (
                    not question_number
                    or question_number in focus_location_by_qn
                    or question_number not in expected_qns
                    or isinstance(image_index, bool)
                    or not isinstance(image_index, int)
                    or not 1 <= image_index <= len(evidence_paths)
                    or not isinstance(bbox, list)
                    or len(bbox) != 4
                    or any(
                        isinstance(value, bool) or not isinstance(value, int)
                        for value in bbox
                    )
                    or not (0 <= bbox[0] < bbox[2] <= 1000)
                    or not (0 <= bbox[1] < bbox[3] <= 1000)
                ):
                    return {}
                focus_location_by_qn[question_number] = location
            if set(focus_location_by_qn) != set(expected_qns):
                return {}
        answer_by_qn = {
            _normalize_question_number(str(answer.get("question_number") or "")): answer
            for answer in student_answers
        }
        blocked = {
            _normalize_question_number(str(question_number))
            for question_number in (blocked_question_numbers or set())
            if _normalize_question_number(str(question_number))
        }
        baseline_by_qn = {
            _normalize_question_number(str(question.get("question_number") or "")): question
            for question in baseline_questions
        }
        targets = []
        for question_number in expected_qns:
            answer = answer_by_qn.get(question_number, {})
            baseline_question = baseline_by_qn.get(question_number, {})
            if (
                question_number not in blocked
                and str(answer.get("answer_state") or "").strip().lower()
                == "unreadable"
                and str(baseline_question.get("question_type") or "")
                .strip()
                .lower()
                == "choice"
            ):
                targets.append(question_number)
                break
        if not targets:
            return {}

        def normalized_hints(block: object) -> list[str]:
            if not isinstance(block, dict) or not isinstance(
                block.get("question_number_hints"), list
            ):
                return []
            return [
                _normalize_question_number(str(value))
                for value in block["question_number_hints"]
                if _normalize_question_number(str(value)) in expected_qns
            ]

        block_geometry = {
            str(block.get("block_id") or ""): block
            for block in (crop_plan["blocks"] if not focus_mode else [])
            if isinstance(block, dict)
        }
        region_by_qn = {
            _normalize_question_number(str(region.get("question_number") or "")): region
            for region in regions
            if isinstance(region, dict)
        }

        def contains(block: dict, region: dict) -> bool:
            block_box = block.get("bbox_1000")
            answer_box = region.get("answer_bbox")
            if (
                not isinstance(block_box, list)
                or len(block_box) != 4
                or not isinstance(answer_box, list)
                or len(answer_box) != 4
            ):
                return False
            left, top, right, bottom = block_box
            answer_left, answer_top, answer_right, answer_bottom = answer_box
            return (
                left <= answer_left
                and top <= answer_top
                and answer_right <= right
                and answer_bottom <= bottom
            )

        def block_for_target(target: str) -> tuple[int, dict] | None:
            target_region = region_by_qn.get(target)
            if target_region is None or len(blocks) != len(evidence_paths):
                return None
            candidates = []
            for index, evidence_block in enumerate(blocks):
                if not isinstance(evidence_block, dict):
                    continue
                geometry = block_geometry.get(
                    str(evidence_block.get("block_id") or "")
                )
                if geometry is None or not contains(geometry, target_region):
                    continue
                hints = normalized_hints(evidence_block)
                block_box = geometry["bbox_1000"]
                area = (block_box[2] - block_box[0]) * (
                    block_box[3] - block_box[1]
                )
                candidates.append((target not in hints, area, index, geometry))
            if not candidates:
                return None
            _missing_hint, _area, index, geometry = min(candidates)
            return index, geometry

        def focus_for_target(target: str) -> tuple[int, str] | None:
            target_location = focus_location_by_qn.get(target)
            if target_location is None:
                return None
            image_index = target_location["image_index"]
            witness_candidates = [
                question_number
                for question_number in expected_qns
                if question_number != target
                and focus_location_by_qn[question_number]["image_index"]
                == image_index
                and str(answer_by_qn.get(question_number, {}).get("answer_state") or "")
                .strip()
                .lower()
                == "visible"
                and str(answer_by_qn.get(question_number, {}).get("student_answer") or "")
                .strip()
            ]
            if not witness_candidates:
                return None
            return image_index - 1, witness_candidates[0]

        recovered: dict[str, dict] = {}
        from services.analyzer import parse_llm_json
        from services.ocr_service import (
            call_vision_images,
            reset_vision_phase_key,
            set_vision_phase_key,
            validate_visual_transcriptions,
        )

        for target in targets:
            if focus_mode:
                selected = focus_for_target(target)
                if selected is None:
                    continue
                block_index, witness_question = selected
                witness_qns = [witness_question]
                question_order = [
                    question_number
                    for question_number in expected_qns
                    if question_number in {target, witness_question}
                ]
            else:
                selected = block_for_target(target)
                if selected is None:
                    continue
                block_index, geometry = selected
                contained_qns = [
                    question_number
                    for question_number in expected_qns
                    if contains(geometry, region_by_qn.get(question_number, {}))
                ]
                witness_qns = [
                    question_number
                    for question_number in contained_qns
                    if question_number != target
                    and str(answer_by_qn.get(question_number, {}).get("answer_state") or "")
                    .strip()
                    .lower()
                    == "visible"
                    and str(answer_by_qn.get(question_number, {}).get("student_answer") or "").strip()
                ]
                if not witness_qns:
                    continue
                question_order = [
                    question_number
                    for question_number in expected_qns
                    if question_number in {target, witness_qns[0]}
                ]
            prompt = self._build_evidence_only_prompt(
                type_label,
                evidence_strategy=(
                    "confirmed_bbox_focus_sheets"
                    if focus_mode
                    else "continuous_question_crop_plan"
                ),
                question_numbers=question_order,
            )
            if not prompt:
                continue
            phase_token = set_vision_phase_key(
                f"local-evidence-recovery:{target}"
            )
            try:
                raw_result = await call_vision_images(
                    [evidence_paths[block_index]],
                    prompt,
                    max_tokens=2048,
                )
                data = parse_llm_json(raw_result)
                local_answers = validate_visual_transcriptions(data.get("questions", []))
                local_by_qn = {
                    _normalize_question_number(str(answer.get("question_number") or "")): answer
                    for answer in local_answers
                }
                if [
                    _normalize_question_number(str(answer.get("question_number") or ""))
                    for answer in local_answers
                ] != question_order:
                    continue
                witness = local_by_qn.get(witness_qns[0], {})
                original_witness = answer_by_qn[witness_qns[0]]
                if (
                    witness.get("answer_state") != "visible"
                    or str(witness.get("student_answer") or "").strip()
                    != str(original_witness.get("student_answer") or "").strip()
                ):
                    continue
                candidate = local_by_qn.get(target, {})
                if (
                    candidate.get("answer_state") == "visible"
                    and str(candidate.get("student_answer") or "").strip()
                ):
                    recovered[target] = {
                        "student_answer": candidate["student_answer"],
                        "answer_state": "visible",
                        "_ocr_local_recovery": True,
                    }
            except Exception as exc:
                logger.info(
                    "PaperCache: local evidence recovery skipped for Q%s (%s): %s",
                    target,
                    type(exc).__name__,
                    exc,
                )
            finally:
                reset_vision_phase_key(phase_token)
        return recovered

    def _resolve_joint_page_type(
        self,
        data: dict,
        *,
        visual_fingerprint: str = "",
    ) -> tuple[str, list[str]]:
        """Route a student page by its content identity, then check the binding.

        Template pages may be different exams whose printed numbers overlap,
        so the paper-global numbers the model reports are never the identity
        on their own.  Content decides first (:meth:`_content_identity_winner`:
        printed stems, printed page caption, server-side visual fingerprint).
        The numbers, printed labels, caption and page role then only have to
        agree with that page.  Without content evidence the numbers decide
        only when no two pages share the observed printed labels.  Anything
        short of exactly one consistent page goes to teacher review.
        """
        if not isinstance(data, dict):
            raise ValueError("联合页型识别结果必须是 JSON 对象")
        selected_label = str(data.get("page_type_id") or "").strip()
        raw_observed = data.get("observed_question_numbers")
        if not isinstance(raw_observed, (list, tuple)):
            raise ValueError("联合识别缺少 observed_question_numbers")

        observed: list[str] = []
        seen: set[str] = set()
        for raw_question_number in raw_observed:
            question_number = _normalize_question_number(
                str(raw_question_number or "")
            )
            if not question_number:
                continue
            if question_number in seen:
                raise ValueError("联合识别返回了重复题号")
            seen.add(question_number)
            observed.append(question_number)
        if not observed and not self._non_graded_pages:
            raise ValueError("联合识别没有返回可验证的印刷题号")

        observed_page_label = _normalize_page_label_text(data.get("observed_page_label"))
        raw_printed = data.get("observed_printed_labels")
        observed_printed = {
            label
            for label in (
                _normalize_page_label_text(value)
                for value in (raw_printed if isinstance(raw_printed, list) else [])
            )
            if label
        }
        content_label = self._content_identity_winner(
            data, observed_page_label, visual_fingerprint,
        )

        # Binding gates: which pages the reported numbers, printed labels,
        # caption and role are consistent with.  They never pick a page alone.
        consistent = []
        for label in self._routable_page_labels():
            identity = self._page_identities.get(label) or {}
            page_label = _normalize_page_label_text(identity.get("printed_page_label"))
            if label in self._non_graded_pages:
                # A page without contract numbers is never chosen by default:
                # an unreadable answer page must not silently become ungraded.
                if observed or selected_label != label or content_label != label:
                    continue
            else:
                question_numbers = set(self._expected_question_numbers(label))
                if not observed or not set(observed).issubset(question_numbers):
                    continue
            printed_labels = self._printed_label_set(label)
            if printed_labels and observed_printed and not observed_printed <= printed_labels:
                continue
            if page_label and observed_page_label and page_label != observed_page_label:
                continue
            consistent.append(label)

        if content_label:
            if content_label not in consistent:
                raise ValueError("试卷内容与模型返回的题号或页型不一致，需人工复核")
            resolved = content_label
        else:
            # Numbers alone identify a page only when its printed labels are
            # unique in this template (one exam numbered across its pages).
            # Two exams that both print "1." need content evidence.
            label_fit = [
                label for label in self._baselines
                if self._printed_label_set(label)
                and (not observed_printed or observed_printed <= self._printed_label_set(label))
            ]
            if len(label_fit) > 1 and self._printed_labels_overlap(label_fit):
                raise ValueError("多页模板的印刷题号重复且缺少试卷内容证据，需人工复核")
            if len(consistent) != 1:
                raise ValueError("题号不能唯一匹配已确认页型，需人工复核")
            resolved = consistent[0]
        if selected_label and selected_label not in self._baselines and selected_label not in self._non_graded_pages:
            raise ValueError("模型返回了不存在的页型，需人工复核")
        if selected_label and selected_label != resolved:
            raise ValueError("模型返回的页型与题号锚点冲突，需人工复核")
        return resolved, observed

    def _routable_page_labels(self) -> list[str]:
        return sorted(
            set(self._baselines) | set(self._non_graded_pages),
            key=lambda value: _question_number_sort_key(str(value)),
        )

    def _printed_label_set(self, label: str) -> set[str]:
        identity = self._page_identities.get(label) or {}
        return {
            normalized
            for normalized in (
                _normalize_page_label_text(value)
                for value in identity.get("printed_question_labels") or []
            )
            if normalized
        }

    def _printed_labels_overlap(self, labels: list[str]) -> bool:
        seen: set[str] = set()
        for label in labels:
            printed = self._printed_label_set(label)
            if seen & printed:
                return True
            seen |= printed
        return False

    def _content_identity_winner(
        self,
        data: dict,
        observed_page_label: str,
        visual_fingerprint: str,
    ) -> str:
        """Return the one template page the student page's content belongs to.

        Three independent content signals, each silent unless decisive:

        * printed stems -- ``observed_stem_snippets`` (openings of the printed
          questions the model sees) matched server-side against each page's
          confirmed question text, which is never sent to the model;
        * the printed page caption (exam title) against the teacher's label;
        * the coarse visual fingerprint, only with a clear margin.

        Signals that point to different pages send the page to teacher review.
        An empty result means there is no content evidence at all.
        """
        winners: set[str] = set()
        labels = self._routable_page_labels()

        caption_matches = [
            label for label in labels
            if observed_page_label and _normalize_page_label_text(
                (self._page_identities.get(label) or {}).get("printed_page_label")
            ) == observed_page_label
        ]
        if len(caption_matches) == 1:
            winners.add(caption_matches[0])

        raw_snippets = data.get("observed_stem_snippets")
        snippets = [
            text for text in (
                _content_text(value)
                for value in (raw_snippets if isinstance(raw_snippets, list) else [])[:12]
            )
            if len(text) >= _MIN_STEM_SNIPPET_CHARS
        ]
        if snippets:
            stems = {
                label: [
                    text for text in (
                        _content_text(question.get("question_text"))
                        for question in self._baselines[label]["ocr_result"].get("questions", [])
                    )
                    if text
                ]
                for label in self._baselines
            }
            hits = {label: 0 for label in stems}
            for snippet in snippets:
                for label, page_stems in stems.items():
                    if _snippet_matches_stems(snippet, page_stems):
                        hits[label] += 1
            ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
            top_label, top_hits = ranked[0] if ranked else ("", 0)
            runner_up = ranked[1][1] if len(ranked) > 1 else 0
            if top_hits >= 1 and top_hits >= 2 * runner_up:
                winners.add(top_label)

        if visual_fingerprint:
            from services.template_page_identity import visual_winner
            visual_label = visual_winner(visual_fingerprint, {
                label: identity["visual_fingerprint"]
                for label, identity in self._page_identities.items()
                if identity.get("visual_fingerprint")
            })
            if visual_label:
                winners.add(visual_label)

        if len(winners) > 1:
            raise ValueError("试卷内容特征指向不同页型，需人工复核")
        return next(iter(winners), "")
