"""Transcribing the student's answers on one page against the frozen answer contract.

``_PaperCacheAnswersMixin`` holds ``PaperCache._recognize_answers_only``: it sends
the page (or its locally generated evidence) to the OCR model, routes it to a
template page when asked, and binds each answer to its contracted question; the
evidence strategies in ``_SPLIT_TRANSCRIPTION_EVIDENCE_STRATEGIES`` keep a
separate grading handoff.  Each answer stays fail-closed on its own:

- ``_adjacent_answer_ambiguity`` sends two rows to review when one visual answer
  may be split across them;
- ``_enforce_question_type_answer_constraints`` applies the frozen answer grammar
  before grading;
- a fill-in answer (``is_fill_blank_question``) longer than
  ``MAX_FILL_BLANK_LENGTH`` is marked wrong as likely working rather than an answer.

Moved unchanged from services/paper_cache.py, which still exposes these names.
"""

import asyncio
import logging
import re

from services.paper_cache_base import (
    _VISUAL_GRADING_EVIDENCE_STRATEGIES,
    _normalize_question_number,
)
from services.paper_cache_review import (
    _transcription_contract_failure,
    _transcription_receipt,
)

# services.paper_cache's logger, so these records keep their logger name.
logger = logging.getLogger("services.paper_cache")


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


class _PaperCacheAnswersMixin:
    """PaperCache's method for transcribing the answers on one student page."""

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
