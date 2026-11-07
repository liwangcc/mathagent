"""Recovering a small OCR miss from the evidence crop it was already read from.

``_PaperCacheRecoveryMixin`` holds ``PaperCache._recover_unreadable_evidence_answers``:
when an answer came back unreadable but the same continuous crop shows a visible
answer, it sends one more transcription-only request for that crop.  The
recovery never replaces a whole page result when the crop or its source pixels
are incomplete, and blocked questions are left alone.

Moved unchanged from services/paper_cache.py, which still exposes PaperCache.
"""

import logging

from services.paper_cache_base import _normalize_question_number

# services.paper_cache's logger, so these records keep their logger name.
logger = logging.getLogger("services.paper_cache")


class _PaperCacheRecoveryMixin:
    """PaperCache's method for recovering a small OCR miss from its evidence crop."""

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
