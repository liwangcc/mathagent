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
from services.paper_cache_routing import (
    _PaperCacheRoutingMixin,
    _PRINTED_LABEL_EDGE_PUNCTUATION,
    _normalize_page_label_text,
    _MIN_STEM_SNIPPET_CHARS,
    _STEM_BIGRAM_CONTAINMENT,
    _content_text,
    _snippet_matches_stems,
)
from services.paper_cache_answers import (
    _PaperCacheAnswersMixin,
    _adjacent_answer_ambiguity,
    MAX_FILL_BLANK_LENGTH,
    _SPLIT_TRANSCRIPTION_EVIDENCE_STRATEGIES,
    is_fill_blank_question,
    _enforce_question_type_answer_constraints,
)
from services.paper_cache_recovery import (
    _PaperCacheRecoveryMixin,
)

logger = logging.getLogger(__name__)


class PaperCache(
    _PaperCacheReviewMixin,
    _PaperCachePromptsMixin,
    _PaperCacheRoutingMixin,
    _PaperCacheAnswersMixin,
    _PaperCacheRecoveryMixin,
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
