"""Visible-ink pages must never come back as a silent all-[无法识别] success.

Regression for production papers IMG_20260913_172140 / IMG_20260913_172211
(outputs/recognize-fail-20260929/ocr-evidence.json):

- 172140: every contracted question carried ``slot_identity_error: "student_slots
  missing for contracted question"``.  That message is only produced when the
  model response has no list-typed ``student_slots``, so the transcription
  contract failed for the whole page, yet the page was accepted as a success.
- 172211: the local-evidence route failed, and the fail-closed wrapper dropped
  the underlying ``error_info``, so the persisted failure had no diagnostic id.

The model boundary is synthetic here. Responses carry only the failure shape
and never invented student answers.
"""
import asyncio
import hashlib
import json
import re

import pytest
from PIL import Image

from services.paper_cache import PaperCache
from services.template_slots import make_slot_ids

ID = re.compile(r"[a-f0-9]{32}")
# Page contract of the production worksheet (question number, type, text slots).
PAGE = [("1", "fill", 6), ("2", "solve", 8), ("3", "choice", 1), ("4", "fill", 5), ("5", "solve", 1)]


def _contract():
    questions = []
    for number, question_type, count in PAGE:
        answers = ["A"] if question_type == "choice" else [f"{number}-{i}" for i in range(1, count + 1)]
        questions.append({
            "question_no": number,
            "question_text": f"第{number}题",
            "question_type": question_type,
            "standard_answer": "；".join(answers),
            "answer_slots": answers,
            "blank_count": count,
            "slot_ids": make_slot_ids(number, count),
            "region_kinds": ["answer"] * count,
            **({"choice_mode": "single"} if question_type == "choice" else {}),
        })
    return questions


def _answer_map():
    return {
        question["question_no"]: {
            **{key: question[key] for key in (
                "question_type", "standard_answer", "answer_slots", "blank_count", "slot_ids", "region_kinds",
            ) + (("choice_mode",) if "choice_mode" in question else ())},
            "question_text": question["question_text"],
            "matched": True,
        }
        for question in _contract()
    }


def _slots(number, count, state):
    return [
        {"slot_id": slot_id, "student_answer": "[无法识别]" if state == "unreadable" else "", "answer_state": state}
        for slot_id in make_slot_ids(number, count)
    ]


def _legacy_scalar_response():
    """The shape proven by the 172140 receipt: questions without student_slots."""
    return {"questions": [
        {"question_number": number, "student_answer": "[无法识别]", "answer_state": "unreadable"}
        for number, _type, _count in PAGE
    ]}


def _slot_response(state):
    return {"questions": [
        {"question_number": number, "student_slots": _slots(number, count, state)}
        for number, _type, count in PAGE
    ]}


@pytest.fixture
def evidence_png(tmp_path):
    path = tmp_path / "text-column-1.png"
    Image.new("RGB", (40, 90), "white").save(path)
    return path


def _route_through_local_evidence(monkeypatch, evidence_png, response):
    """Drive the production acceptance point with a synthetic model response."""
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": _contract()}])
    sent = {}

    async def vision(paths, prompt, **request):
        sent.update(paths=list(paths), prompt=prompt, request=request)
        return json.dumps(response, ensure_ascii=False)

    async def local_evidence(*_args, **_kwargs):
        return await cache._recognize_answers_only(
            str(evidence_png), "page_1",
            evidence_mode=True,
            evidence_strategy="full_column_difference",
            evidence_paths=(str(evidence_png),),
        )

    monkeypatch.setattr("services.ocr_service.call_vision_images", vision)
    cache._template_evidence_matcher = object()
    cache._require_local_evidence = True
    cache._recognize_with_local_evidence = local_evidence
    result = asyncio.run(cache.recognize_page("paper.png"))
    return result, sent


def test_whole_page_slot_contract_violation_is_failed_with_diagnostic_id(monkeypatch, evidence_png):
    result, sent = _route_through_local_evidence(monkeypatch, evidence_png, _legacy_scalar_response())

    assert sent["request"]["max_attempts"] == 1  # evidence mode never re-asks the model
    assert result["questions"] == []
    assert result["error"].startswith("答案转录未按作答位合同返回任何可用作答")
    info = result["error_info"]
    assert info["code"] == "OUTPUT_PARSE_FAILED" and info["stage"] == "transcription"
    assert ID.fullmatch(info["error_id"]) and ID.fullmatch(info["correlation_id"])
    assert result["_ocr_retryable"] is False
    assert result["_ocr_error_code"] == "transcription_slot_contract_violated"
    # Routed review placeholders: never invented text, never positional answers.
    placeholders = result["_fail_closed_questions"]
    assert [q["question_no"] for q in placeholders] == ["1", "2", "3", "4", "5"]
    assert all(q["slot_identity_error"] == "ocr_failed" for q in placeholders)
    assert all(set(q["student_answer"].split("；")) == {"[无法识别]"} for q in placeholders)
    # The receipt names the exact bytes sent and proves the response shape.
    receipt = result["_ocr_receipt"]
    assert receipt["images"] == [{
        "sha256": hashlib.sha256(evidence_png.read_bytes()).hexdigest(),
        "bytes": evidence_png.stat().st_size, "width": 40, "height": 90,
    }]
    assert ID.fullmatch(receipt["response_sha256"][:32])
    assert [q["student_slots"] for q in receipt["questions"]] == ["absent"] * 5
    assert "[无法识别]" not in json.dumps(receipt, ensure_ascii=False)


def test_valid_slots_that_read_nothing_are_transcription_empty(monkeypatch, evidence_png):
    result, _ = _route_through_local_evidence(monkeypatch, evidence_png, _slot_response("unreadable"))

    assert result["questions"] == []
    assert result["error"].startswith("答案转录未读出本页任何作答")
    assert result["error_info"]["code"] == "TRANSCRIPTION_EMPTY"
    assert result["_ocr_error_code"] == "transcription_zero_readout"
    assert result["_ocr_receipt"]["questions"][1]["slot_states"] == {"unreadable": 8}


def test_real_readouts_are_accepted_and_blank_claims_are_scored_as_blank(monkeypatch, evidence_png):
    response = _slot_response("blank")
    response["questions"][0]["student_slots"][0] = {
        "slot_id": "1[1]", "student_answer": "2/5", "answer_state": "visible",
    }
    response["questions"][3]["student_slots"] = _slots("4", 5, "unreadable")

    accepted, _ = _route_through_local_evidence(monkeypatch, evidence_png, response)

    assert "error" not in accepted
    by_number = {q["question_number"]: q for q in accepted["questions"]}
    assert by_number["1"]["student_slots"][0]["student_answer"] == "2/5"
    assert by_number["1"]["student_slots"][1]["answer_state"] == "blank"  # partly answered: unchanged
    assert by_number["3"]["answer_state"] == by_number["5"]["answer_state"] == "blank"  # single boxes
    # Every box of question 2 claimed blank: product ruling 2026-09-30
    # ("unclear handwriting is judged wrong") scores the claim as blank
    # (unanswered -> wrong) instead of routing faint strokes to teacher
    # review.  Explicit unreadable slots (question 4) keep needs-review.
    assert all(slot["answer_state"] == "blank" for slot in by_number["2"]["student_slots"])
    assert by_number["4"]["answer_state"] == "unreadable"
    assert "_blank_claim_review_qns" not in accepted
    assert accepted["_ocr_receipt"]["questions"][1]["slot_states"] == {"blank": 8}
    assert accepted["_ocr_receipt"]["images"][0]["width"] == 40


def test_page_read_as_entirely_blank_is_graded_as_unanswered(monkeypatch, evidence_png):
    # Product ruling 2026-09-30 ("unclear handwriting is judged wrong"): a page
    # the model reports entirely blank is graded as unanswered (wrong) instead
    # of going to teacher review.  The explicit-unreadable page shape still
    # fails loudly as a zero readout (see the TRANSCRIPTION_EMPTY test above).
    result, _ = _route_through_local_evidence(monkeypatch, evidence_png, _slot_response("blank"))

    assert "error" not in result
    assert len(result["questions"]) == 5
    assert all(q["answer_state"] == "blank" for q in result["questions"])


def test_partial_contract_violation_stays_per_question_fail_closed(monkeypatch, evidence_png):
    response = _slot_response("blank")
    response["questions"][0]["student_slots"][0] = {
        "slot_id": "1[1]", "student_answer": "2/5", "answer_state": "visible",
    }
    response["questions"][1] = {"question_number": "2", "student_answer": "8", "answer_state": "visible"}

    result, _ = _route_through_local_evidence(monkeypatch, evidence_png, response)

    assert "error" not in result
    by_number = {q["question_number"]: q for q in result["questions"]}
    assert by_number["2"]["slot_identity_error"] == "student_slots missing for contracted question"
    assert by_number["2"]["answer_state"] == "unreadable"
    assert by_number["1"]["student_slots"][0]["student_answer"] == "2/5"
    assert result["_slot_contract_violations"] == ["2"]


def test_rejected_local_evidence_keeps_underlying_diagnostic_id():
    from services.processing_errors import report_failure

    source_info = report_failure(ValueError("synthetic transcription contract failure"), stage="transcription")
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": _contract()}])

    async def rejected(*_args, **_kwargs):
        return {"questions": [], "error": source_info["message"], "error_info": source_info,
                "_ocr_retryable": False, "_ocr_error_code": "local_processing_failed"}

    cache._template_evidence_matcher = object()
    cache._require_local_evidence = True
    cache._recognize_with_local_evidence = rejected
    result = asyncio.run(cache.recognize_page("paper.png"))

    assert result["error"] == "本次模板答案证据未通过校验，已停止自动批改该页"
    assert result["error_info"] == source_info


@pytest.mark.parametrize(("state", "has_diagnostic"), [("fault", True), ("pending", False), ("unavailable", False)])
def test_evidence_failure_without_diagnostic_gets_one_but_pauses_do_not(state, has_diagnostic):
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": _contract()}])

    async def accelerator(*_args, **_kwargs):
        return {"error": "底层失败", "_accelerator_state": state,
                "_accelerator_reason_code": "server_template_evidence_failed"}

    cache._template_evidence_matcher = object()
    cache._require_local_evidence = True
    cache._recognize_with_local_evidence = accelerator
    result = asyncio.run(cache.recognize_page("paper.png"))

    assert ("error_info" in result) is has_diagnostic
    if has_diagnostic:
        assert result["error_info"]["stage"] == "image"
        assert ID.fullmatch(result["error_info"]["error_id"])
        assert ID.fullmatch(result["error_info"]["correlation_id"])


def _anonymous_batch(tmp_path, image_name="page.jpg"):
    from services.quick_grader import QuickBatch

    batch = QuickBatch("abc123def456", str(tmp_path))
    batch.identity_mode = "anonymous"
    image = tmp_path / image_name
    image.write_bytes(b"page")
    batch.uploads = [{"paper_id": "paper-a", "filename": image_name, "image_path": str(image),
                      "original_image_path": str(image), "source_sha256": "1" * 64, "working_sha256": "1" * 64}]
    return batch, str(image)


@pytest.mark.parametrize("template_placeholders", ["single_page", "multi_page"])
def test_zero_readout_persists_as_failed_review_with_diagnostic_id(
    monkeypatch, evidence_png, tmp_path, template_placeholders,
):
    import routers.grade as grade
    from services import grader

    failure, _ = _route_through_local_evidence(monkeypatch, evidence_png, _legacy_scalar_response())
    batch, image_path = _anonymous_batch(tmp_path)
    # Multi-page templates have no unrouted placeholders; the routed page does.
    template_rows = [] if template_placeholders == "multi_page" else failure["_fail_closed_questions"]

    payload = grade._ingest_recognized_paper(
        batch, None, 1, "IMG_20260913_172140.jpg", image_path, failure, failure_questions=template_rows,
    )

    assert payload["requires_manual_review"] is True
    assert payload["error_info"]["error_id"] == failure["error_info"]["error_id"]
    paper = batch.papers[0]
    assert paper["ocr_failure"] is True
    assert paper["error_info"]["error_id"] == failure["error_info"]["error_id"]
    assert paper["ocr_receipt"]["questions"][0]["student_slots"] == "absent"
    assert [q["question_no"] for q in paper["questions"]] == ["1", "2", "3", "4", "5"]
    correlation_id = failure["error_info"]["correlation_id"]
    assert f"诊断编号 {correlation_id}" in grade._recognition_paper_payload(batch, paper)["error"]

    sent = []

    class ImmediateQueue:
        async def submit(self, coro_factory, **_kwargs):
            return await coro_factory()

    async def judge(questions, **_kwargs):
        sent.extend(questions)
        return {}

    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    graded = asyncio.run(grader.grade_paper_unified(paper, _answer_map(), "匿名页 1", 1, category="daily"))

    assert sent == []  # nothing unreadable is ever sent to be judged
    parents = {str(detail.get("parent_question_no") or detail["question_no"]).split("(")[0]
               for detail in graded["details"]}
    assert parents == {"1", "2", "3", "4", "5"}
    assert graded["needs_review_count"] == graded["total"] > 0
    assert graded["correct_count"] == graded["wrong_count"] == graded["partial_count"] == 0
    for detail in graded["details"]:
        assert detail["result"] == "needs_review"
        assert f"诊断编号 {correlation_id}" in json.dumps(detail, ensure_ascii=False)


def test_all_blank_multi_slot_question_does_not_reject_the_page():
    """A skipped 6-box question binds to "；；；；；" with state blank; that is empty."""
    from services.ocr_service import _multi_pool_response_validator, validate_visual_transcriptions
    from services.template_slots import slot_transcription_prompt

    questions = [{"question_number": q["question_no"], **q} for q in _contract()]
    prompt = "[task=student_transcription_only] student_answer answer_state" + slot_transcription_prompt(questions)
    response = _slot_response("blank")
    response["questions"][2]["student_slots"] = [
        {"slot_id": "3[1]", "student_answer": "A", "answer_state": "visible"},
    ]
    # The strict scheduler validator is what used to reject the whole page.
    _multi_pool_response_validator(prompt)(json.dumps(response, ensure_ascii=False))
    bound = validate_visual_transcriptions(
        response["questions"],
        slot_contracts={q["question_no"]: q["slot_ids"] for q in _contract()},
    )
    assert bound[0]["answer_state"] == "blank"
    assert [slot["answer_state"] for slot in bound[0]["student_slots"]] == ["blank"] * 6
    # Real contradictions still fail closed, for that question only.
    for contradiction in (
        {"question_number": "9", "student_answer": "12", "answer_state": "blank"},
        {"question_number": "9", "student_answer": "", "answer_state": "visible"},
        {"question_number": "9", "student_answer": "[无法识别]", "answer_state": "visible"},
    ):
        bound_page = validate_visual_transcriptions([
            {"question_number": "8", "student_answer": "x=2", "answer_state": "visible"},
            contradiction,
        ])
        assert bound_page[0]["student_answer"] == "x=2"
        assert bound_page[1]["answer_state"] == "unreadable"
        assert bound_page[1]["student_answer"] == "[无法识别]"
    assert validate_visual_transcriptions(
        [{"question_number": "9", "student_answer": "；；", "answer_state": "blank"}],
    )[0]["answer_state"] == "blank"
