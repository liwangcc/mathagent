"""Multi-page template contract: paper-global question numbers and page roles.

Fix (a): every page's hierarchy model numbers its questions from "1", so a
merge step shifts each page by the question count of all earlier pages.
Fix (b): a student page is routed by CONTENT identity.  Case B (primary): two
template pages are different exams that both print "1.", "2." -- a page from
exam A must reach page 1 and a page from exam B page 2, never the other.  Case
A (secondary): a teacher-chosen ``page_role`` keeps an answer sheet and a
question page of one exam apart.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from PIL import Image, ImageDraw

from services import template_directory_pipeline as pipeline
from services import template_one_three as one_three
from services.paper_cache import PaperCache
from services.template_page_identity import compute_visual_fingerprint, fingerprint_distance
from services.template_page_numbering import globalize_page_numbers, validate_global_numbering
from services.template_session import (
    TemplateSession,
    answer_contract_pages,
    insert_question,
    page_role_of,
)
from tests.test_template_session import (  # noqa: F401 - autouse readiness fixture
    _client,
    _make_app_and_session,
    _parse_sse,
    _patch_template_layout,
    _ready_batch_grade_chain,
    _template_png_bytes,
)
from tests.test_template_evidence import (
    QUESTIONS as EVIDENCE_QUESTIONS,
    _perspective_student,
    _synthetic_template,
)
from tests.test_template_shared_slots import (
    HIERARCHY_PHASE,
    LOCATOR_PHASE,
    PAPER_B_KINDS,
    locator_payload,
    paper_b_wire,
    run_pipeline,
    solver_payload,
    transcription_payload,
)
from tests.grade_family import set_grade_global
from routers import grade_recognition
from routers import grade_template_import


# ---------------------------------------------------------------------------
# Fix (a): deterministic page-local -> paper-global renumbering
# ---------------------------------------------------------------------------
def _real_page_result(tmp_path, monkeypatch) -> dict:
    """Run the real v4 page pipeline offline; it numbers the page from 1."""
    source = tmp_path / "page.png"
    Image.new("RGB", (600, 800), "white").save(source)
    wire = paper_b_wire()
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(wire))
    payloads = {
        LOCATOR_PHASE: locator_payload(hierarchy, kinds=PAPER_B_KINDS),
        one_three.SOLVE_PHASE: solver_payload(hierarchy),
        one_three.TRANSCRIBE_PHASE: transcription_payload(hierarchy),
    }

    async def request(images, prompt, phase, validator, **kwargs):
        if phase == HIERARCHY_PHASE:
            return validator(copy.deepcopy(wire))
        if phase == "template-coarse-locator-v1":
            raise ValueError("coarse hints unavailable")
        return validator(one_three.parse_json(json.dumps(payloads[phase], ensure_ascii=False)))

    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source)
    assert "error" not in result
    return result


def test_globalize_rewrites_numbers_slots_anchors_and_directory_nodes(tmp_path, monkeypatch):
    local = _real_page_result(tmp_path, monkeypatch)
    snapshot = copy.deepcopy(local)
    assert [q["question_no"] for q in local["questions"]] == ["1", "2"]

    merged = globalize_page_numbers(local, 3, page_index=1)

    assert local == snapshot, "the merge step must not mutate its input"
    first, second = merged["questions"]
    assert [q["question_no"] for q in merged["questions"]] == ["4", "5"]
    assert [q["question_number"] for q in merged["questions"]] == ["4", "5"]
    assert [q["q_id"] for q in merged["questions"]] == ["Q4", "Q5"]
    assert [q["page_local_number"] for q in merged["questions"]] == ["1", "2"]
    assert first["slot_ids"] == ["4[1]", "4[2]", "4[3]"]
    assert second["slot_ids"] == ["5[1]", "5[2]", "5[3]", "5[4]"]
    # Printed provenance is never renumbered.
    assert first["printed_path"] == ["二、解答题", "3."]
    assert second["printed_path"] == ["二、解答题", "6."]
    assert first["directory_node_id"] == "p2-n001"
    assert second["directory_node_id"] == "p2-n002"
    assert first["directory_metadata"]["node_id"] == "p2-n001"
    assert first["directory_metadata"]["question_number"] == "4"
    assert first["directory_metadata"]["slot_ids"] == ["4[1]", "4[2]", "4[3]"]
    assert [row["slot_id"] for row in second["slot_binding"]["mapping"]] == second["slot_ids"]

    directory = merged["question_directory"]
    assert [node["node_id"] for node in directory["nodes"]] == ["p2-n001", "p2-n002"]
    assert [leaf["question_number"] for leaf in directory["leaves"]] == ["4", "5"]
    assert [leaf["q_id"] for leaf in directory["leaves"]] == ["Q4", "Q5"]
    assert [leaf["page_local_number"] for leaf in directory["leaves"]] == ["1", "2"]
    assert directory["leaves"][1]["slot_ids"] == ["5[1]", "5[2]", "5[3]", "5[4]"]
    assert directory["question_number_offset"] == 3

    assert [a["question_number"] for a in merged["question_anchors"]] == ["4", "5"]
    assert merged["question_anchors"][0]["question_number_bbox"] == (
        local["question_anchors"][0]["question_number_bbox"])
    assert [r["question_number"] for r in merged["template_answer_regions"]] == ["4", "5"]
    assert merged["template_answer_regions"][1]["slot_ids"] == ["5[1]", "5[2]", "5[3]", "5[4]"]
    assert merged["template_answer_regions"][1]["regions"] == local["template_answer_regions"][1]["regions"]
    assert [r["question_number"] for r in merged["template_handwriting_regions"]] == ["4", "5"]

    # The rewritten questions still satisfy the frozen slot contract, and the
    # page-local number survives the teacher-confirmation shape.
    page = TemplateSession.make_page(1, merged["questions"])
    assert [q["slot_ids"] for q in page["questions"]] == [first["slot_ids"], second["slot_ids"]]
    assert [q["page_local_number"] for q in page["questions"]] == ["1", "2"]
    first_page = TemplateSession.make_page(0, [
        {"question_no": str(n), "question_text": f"q{n}"} for n in (1, 2, 3)
    ])
    assert validate_global_numbering([page, first_page]) == ["1", "2", "3", "4", "5"]


def test_globalize_pending_review_list_follows_the_numbers(tmp_path, monkeypatch):
    local = _real_page_result(tmp_path, monkeypatch)
    local["requires_teacher_confirmation"] = ["2"]
    local["anchor_diagnostics"]["review_reasons"] = ["2", "2 作答框数量 3 与实际框数 2 不一致"]
    merged = globalize_page_numbers(local, 10, page_index=2)
    assert merged["requires_teacher_confirmation"] == ["12"]
    # Only exact question numbers move; free-text reasons stay verbatim.
    assert merged["anchor_diagnostics"]["review_reasons"] == ["12", "2 作答框数量 3 与实际框数 2 不一致"]
    assert merged["questions"][0]["directory_node_id"] == "p3-n001"


def _minimal_page(numbers, *, slot=True):
    return {"questions": [
        {"question_number": n, "question_text": f"题{n}", "question_type": "fill",
         "standard_answer": "1", "answer_slots": ["1"],
         **({"slot_ids": [f"{n}[1]"]} if slot else {})}
        for n in numbers
    ]}


@pytest.mark.parametrize("numbers", [["1", "3"], ["2", "3"], ["1", "1"], ["1", "2.1"]])
def test_globalize_fails_closed_on_non_local_numbering(numbers):
    with pytest.raises(ValueError):
        globalize_page_numbers(_minimal_page(numbers), 4, page_index=1)


def test_globalize_fails_closed_on_foreign_slot_and_double_offset():
    page = _minimal_page(["1", "2"])
    page["questions"][1]["slot_ids"] = ["1[2]"]
    with pytest.raises(ValueError, match="作答位"):
        globalize_page_numbers(page, 2, page_index=1)

    once = globalize_page_numbers(_minimal_page(["1", "2"]), 2, page_index=1)
    with pytest.raises(ValueError):
        globalize_page_numbers(once, 2, page_index=1)
    with pytest.raises(ValueError):
        globalize_page_numbers(_minimal_page(["1"]), -1, page_index=0)

    region_page = _minimal_page(["1"])
    region_page["template_answer_regions"] = [{"question_number": "7", "regions": [[1, 1, 2, 2]]}]
    with pytest.raises(ValueError):
        globalize_page_numbers(region_page, 0, page_index=0)


def test_validate_global_numbering_rejects_collision_and_gap():
    page_one = TemplateSession.make_page(0, [{"question_no": "1"}, {"question_no": "2"}])
    colliding = TemplateSession.make_page(1, [{"question_no": "1"}, {"question_no": "2"}])
    with pytest.raises(ValueError, match="重复"):
        validate_global_numbering([page_one, colliding])
    gap = TemplateSession.make_page(1, [{"question_no": "4"}])
    with pytest.raises(ValueError, match="连续"):
        validate_global_numbering([page_one, gap])


def _two_page_pipeline(calls):
    """Each page is recognized alone and, like the real model, numbered from 1."""

    async def fake_pipeline(_path, **_kwargs):
        calls.append(_path)
        questions, regions, anchors = [], [], []
        for number in ("1", "2"):
            top = 120 + int(number) * 100
            questions.append({
                "question_number": number, "question_no": number, "q_id": "Q" + number,
                "question_text": f"第{len(calls)}页第{number}题", "question_type": "fill",
                "standard_answer": number, "answer_slots": [number], "slot_ids": [f"{number}[1]"],
                "printed_path": [f"{number}."], "directory_node_id": f"n00{number}",
                "directory_parent_id": None,
            })
            regions.append({"question_number": number, "regions": [[500, top, 850, top + 60]],
                            "slot_ids": [f"{number}[1]"]})
            anchors.append({"question_number": number, "question_number_bbox": [60, top, 100, top + 30]})
        return {"questions": questions, "template_answer_regions": regions,
                "question_anchors": anchors}

    return fake_pipeline


def test_multi_page_template_import_confirm_and_build_have_unique_numbers(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    calls = []
    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _two_page_pipeline(calls))
    client = _client(Session, monkeypatch)

    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "daily"},
        files=[
            ("files", ("page-1.png", _template_png_bytes("P1"), "image/png")),
            ("files", ("page-2.png", _template_png_bytes("P2"), "image/png")),
        ],
    )
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"], events
    template_id = next(payload for event, payload in events if event == "done")["template_id"]
    assert len(calls) == 2

    sess = TemplateSession.load_meta(template_id, str(tmp_path))
    assert [[q["question_no"] for q in page["questions"]] for page in sess.pages] == [["1", "2"], ["3", "4"]]
    assert [q["page_local_number"] for q in sess.pages[1]["questions"]] == ["1", "2"]
    assert [q["slot_ids"] for q in sess.pages[1]["questions"]] == [["3[1]"], ["4[1]"]]
    assert sess.pages[1]["questions"][0]["printed_path"] == ["1."]
    assert [q["directory_node_id"] for q in sess.pages[0]["questions"]] == ["p1-n001", "p1-n002"]
    assert [q["directory_node_id"] for q in sess.pages[1]["questions"]] == ["p2-n001", "p2-n002"]
    assert sess.page_artifacts["page_2"]["question_numbers"] == ["3", "4"]
    assert [a["question_number"] for a in sess.page_artifacts["page_2"]["question_anchors"]] == ["3", "4"]
    assert all(page_role_of(page) == "answer_page" for page in sess.pages)
    assert sess.pages[1]["page_identity"]["printed_question_labels"] == ["1.", "2."]
    assert sess.pages[1]["page_identity"]["visual_fingerprint"].startswith("dhash256:")

    confirm = client.post("/api/batch-grade/template/confirm", json={
        "template_id": template_id,
        "category": "daily",
        "pages": [
            {"page_index": page["page_index"], "questions": [
                {key: question[key] for key in (
                    "question_no", "question_text", "question_type", "standard_answer",
                    "answer_slots", "blank_count")}
                for question in page["questions"]
            ]}
            for page in sess.pages
        ],
    })
    assert confirm.status_code == 200, confirm.text

    confirmed = TemplateSession.load_meta(template_id, str(tmp_path))
    assert confirmed.recognition_contract_error() == ""
    assert list(confirmed.build_answer_map()) == ["1", "2", "3", "4"]
    assert [q["page_local_number"] for q in confirmed.pages[1]["questions"]] == ["1", "2"]
    assert confirmed.pages[1]["page_role"] == "answer_page"
    cache = gr_router._build_template_paper_cache(confirmed)
    assert cache._expected_question_numbers("page_1") == ["1", "2"]
    assert cache._expected_question_numbers("page_2") == ["3", "4"]


# ---------------------------------------------------------------------------
# Fix (b), case B (primary): two template pages are two DIFFERENT exams
# ---------------------------------------------------------------------------
EXAM_A_STEMS = ["已知函数f(x)=x²+2x，求f(3)的值", "解方程：2x+5=17，求x的值"]
EXAM_B_STEMS = ["一个长方形的长是8厘米，宽是5厘米，求它的面积", "计算：125×8×4的积是多少"]


def _exam_a_image() -> Image.Image:
    return _question_page_image()


def _exam_b_image() -> Image.Image:
    return _answer_sheet_image()


def _two_exam_cache(*, captions=("", ""), fingerprints=True, printed=(("1.", "2."), ("1.", "2."))) -> PaperCache:
    """Exam A on page 1, exam B on page 2; both print "1." and "2.".

    After the merge step exam A owns global numbers 1-2 and exam B 3-4.
    """
    pages = []
    for page_index, (stems, image, caption, labels) in enumerate(zip(
        (EXAM_A_STEMS, EXAM_B_STEMS), (_exam_a_image(), _exam_b_image()), captions, printed,
    )):
        identity = {"printed_page_label": caption, "printed_question_labels": list(labels)}
        if fingerprints:
            identity["visual_fingerprint"] = compute_visual_fingerprint(image)
        pages.append({
            "page_index": page_index, "page_role": "answer_page", "page_identity": identity,
            "questions": [
                {"question_no": str(page_index * 2 + offset + 1), "question_text": stem,
                 "question_type": "solve", "standard_answer": f"answer-{page_index}-{offset}",
                 "page_local_number": str(offset + 1)}
                for offset, stem in enumerate(stems)
            ],
        })
    return PaperCache.from_confirmed_template_pages(pages)


def _openings(stems: list[str], length: int = 12) -> list[str]:
    """What a model reads off a student page: stem openings, spacing/punctuation varied."""
    return [" ".join(stem.replace("，", ",")[:length]) for stem in stems]


@pytest.mark.parametrize("exam,expected_label,numbers,stems", [
    ("A", "page_1", ["1", "2"], EXAM_A_STEMS),
    ("B", "page_2", ["3", "4"], EXAM_B_STEMS),
])
def test_case_b_student_page_routes_to_its_own_exam_by_content(exam, expected_label, numbers, stems):
    cache = _two_exam_cache(fingerprints=False)
    assert cache._expected_question_numbers("page_1") == ["1", "2"]
    assert cache._expected_question_numbers("page_2") == ["3", "4"]
    assert cache._resolve_joint_page_type({
        "page_type_id": expected_label, "observed_question_numbers": numbers,
        "observed_printed_labels": ["1", "2"], "observed_stem_snippets": _openings(stems),
    }) == (expected_label, numbers)


@pytest.mark.parametrize("data,student_image", [
    # Exam B page, but the model bound its printed "1","2" to exam A's numbers.
    ({"page_type_id": "page_1", "observed_question_numbers": ["1", "2"],
      "observed_printed_labels": ["1", "2"], "observed_stem_snippets": _openings(EXAM_B_STEMS)}, None),
    # Exam A page claimed as exam B.
    ({"page_type_id": "page_2", "observed_question_numbers": ["3", "4"],
      "observed_stem_snippets": _openings(EXAM_A_STEMS)}, None),
    # Numbers only: both exams print "1." and "2.", so numbers cannot identify the exam.
    ({"page_type_id": "page_1", "observed_question_numbers": ["1", "2"],
      "observed_printed_labels": ["1", "2"]}, None),
    ({"page_type_id": "page_2", "observed_question_numbers": ["3"]}, None),
    # Stems say exam B while the page visually is exam A: conflicting content.
    ({"page_type_id": "page_2", "observed_question_numbers": ["3", "4"],
      "observed_stem_snippets": _openings(EXAM_B_STEMS)}, "A"),
    # Visually exam B, but the model bound exam A's numbers.
    ({"page_type_id": "page_1", "observed_question_numbers": ["1", "2"]}, "B"),
])
def test_case_b_overlapping_printed_numbers_never_false_match(data, student_image):
    cache = _two_exam_cache()
    fingerprint = ""
    if student_image:
        image = _exam_a_image() if student_image == "A" else _exam_b_image()
        fingerprint = compute_visual_fingerprint(_with_handwriting(image))
    with pytest.raises(ValueError, match="需人工复核"):
        cache._resolve_joint_page_type(data, visual_fingerprint=fingerprint)


def test_case_b_visual_or_caption_identity_routes_without_stems():
    cache = _two_exam_cache()
    student_b = compute_visual_fingerprint(_with_handwriting(_exam_b_image()))
    assert cache._resolve_joint_page_type({
        "page_type_id": "page_2", "observed_question_numbers": ["3", "4"],
        "observed_printed_labels": ["1", "2"],
    }, visual_fingerprint=student_b) == ("page_2", ["3", "4"])

    captioned = _two_exam_cache(captions=("期中考试 A卷", "期中考试 B卷"), fingerprints=False)
    assert captioned._resolve_joint_page_type({
        "page_type_id": "page_1", "observed_question_numbers": ["2"],
        "observed_page_label": "期中考试A卷",
    }) == ("page_1", ["2"])
    with pytest.raises(ValueError, match="需人工复核"):
        captioned._resolve_joint_page_type({
            "page_type_id": "page_1", "observed_question_numbers": ["1"],
            "observed_page_label": "期中考试 B卷",
        })


def test_one_exam_numbered_across_pages_still_routes_by_numbers():
    """Pages of a single exam print distinct numbers, so numbers remain decisive."""
    cache = _two_exam_cache(fingerprints=False, printed=(("1.", "2."), ("3.", "4.")))
    assert cache._resolve_joint_page_type({
        "page_type_id": "page_2", "observed_question_numbers": ["3", "4"],
        "observed_printed_labels": ["3", "4"],
    }) == ("page_2", ["3", "4"])


def test_case_b_prompt_asks_for_content_evidence_without_leaking_stems():
    cache = _two_exam_cache()
    prompt = cache._build_page_and_answers_prompt()
    assert "observed_stem_snippets" in prompt
    assert "不能只凭印刷题号选页型" in prompt
    for stem in EXAM_A_STEMS + EXAM_B_STEMS:
        assert stem not in prompt
        assert stem[:6] not in prompt
    assert "answer-0-0" not in prompt and "answer-1-0" not in prompt


def test_case_b_exam_b_page_binds_answers_to_exam_b_numbers(tmp_path):
    from unittest.mock import AsyncMock, patch

    cache = _two_exam_cache()
    student_path = tmp_path / "student-exam-b.png"
    _with_handwriting(_exam_b_image()).save(student_path)
    response = json.dumps({
        "page_type_id": "page_2", "observed_question_numbers": ["3", "4"],
        "observed_page_label": "", "observed_printed_labels": ["1", "2"],
        "observed_stem_snippets": _openings(EXAM_B_STEMS),
        "questions": [
            {"question_number": "3", "student_answer": "40平方厘米", "answer_state": "visible"},
            {"question_number": "4", "student_answer": "4000", "answer_state": "visible"},
        ],
        "student_name": "", "student_no": "",
    }, ensure_ascii=False)
    with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)), patch(
        "services.ocr_service._prepare_vision_image", new=AsyncMock(return_value=str(student_path)),
    ):
        result = asyncio.run(cache._recognize_answers_only(str(student_path), auto_route=True))

    assert "error" not in result, result
    assert result["_cache_type_label"] == "page_2"
    assert [q["question_number"] for q in result["questions"]] == ["3", "4"]
    assert [q["question_text"] for q in result["questions"]] == EXAM_B_STEMS
    assert [q["student_answer"] for q in result["questions"]] == ["40平方厘米", "4000"]


def test_case_b_exam_a_page_misbound_to_exam_a_numbers_from_exam_b_is_rejected(tmp_path):
    from unittest.mock import AsyncMock, patch

    cache = _two_exam_cache()
    student_path = tmp_path / "student-exam-b.png"
    _with_handwriting(_exam_b_image()).save(student_path)
    # The model reads exam B's printed "1","2" and binds them to exam A.
    response = json.dumps({
        "page_type_id": "page_1", "observed_question_numbers": ["1", "2"],
        "observed_printed_labels": ["1", "2"], "observed_stem_snippets": _openings(EXAM_B_STEMS),
        "questions": [
            {"question_number": "1", "student_answer": "40", "answer_state": "visible"},
            {"question_number": "2", "student_answer": "4000", "answer_state": "visible"},
        ],
        "student_name": "", "student_no": "",
    }, ensure_ascii=False)
    with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)), patch(
        "services.ocr_service._prepare_vision_image", new=AsyncMock(return_value=str(student_path)),
    ):
        result = asyncio.run(cache._recognize_answers_only(str(student_path), auto_route=True))

    assert "error" in result
    assert not result.get("questions")
    assert "_cache_type_label" not in result


def test_case_b_production_artifact_matcher_routes_each_exam_by_content(tmp_path):
    """New templates route through server-side keypoint matching of the page."""
    from services.template_evidence import (
        TemplateEvidenceMatcher,
        build_template_artifact,
        cleanup_evidence,
    )

    template_dir = tmp_path / "stored"
    exams, artifacts = {}, {}
    for page_index, seed in enumerate((7, 91)):
        source = tmp_path / f"exam-{page_index + 1}.png"
        exams[f"page_{page_index + 1}"] = _synthetic_template(source, seed=seed)
        # Both exams print questions "1" and "2"; the merge step made them 1-2 and 3-4.
        questions = copy.deepcopy(EVIDENCE_QUESTIONS)
        for offset, question in enumerate(questions):
            question.pop("question_no", None)
            question["question_number"] = str(page_index * 2 + offset + 1)
        artifacts[f"page_{page_index + 1}"] = build_template_artifact(
            source, template_dir, page_index, questions,
        )
    matcher = TemplateEvidenceMatcher(artifacts, template_dir)

    for label, template in exams.items():
        student = tmp_path / f"student-{label}.png"
        _perspective_student(template, student)
        result = matcher.match_and_build(student, tmp_path / f"evidence-{label}")
        assert result.page_type_id == label
        cleanup_evidence(result.evidence_path)


# ---------------------------------------------------------------------------
# Fix (b), case A (secondary): page roles within one exam
# ---------------------------------------------------------------------------
def _question_page_image() -> Image.Image:
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    for row in range(26):
        top = 40 + row * 28
        draw.rectangle((40, top, 40 + (row * 37) % 300 + 220, top + 10), fill="black")
    return image


def _answer_sheet_image() -> Image.Image:
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 600, 90), fill="black")
    for row in range(5):
        for column in range(2):
            left, top = 40 + column * 280, 140 + row * 130
            draw.rectangle((left, top, left + 240, top + 100), outline="black", width=8)
    return image


def _with_handwriting(image: Image.Image) -> Image.Image:
    copy_image = image.copy()
    draw = ImageDraw.Draw(copy_image)
    draw.line((300, 200, 360, 230), fill="black", width=3)
    draw.line((120, 500, 170, 480), fill="black", width=3)
    return copy_image


def _role_cache() -> PaperCache:
    """A question booklet page and an answer sheet printing the same labels."""
    return PaperCache.from_confirmed_template_pages([
        {"page_index": 0, "questions": [], "page_role": "question_only",
         "page_identity": {"printed_page_label": "试题卷",
                           "visual_fingerprint": compute_visual_fingerprint(_question_page_image())}},
        {"page_index": 1, "page_role": "answer_sheet",
         "page_identity": {"printed_page_label": "答题卡", "printed_question_labels": ["1.", "2."],
                           "visual_fingerprint": compute_visual_fingerprint(_answer_sheet_image())},
         "questions": [
             {"question_no": "1", "question_text": "", "standard_answer": "A", "printed_path": ["1."]},
             {"question_no": "2", "question_text": "", "standard_answer": "B", "printed_path": ["2."]},
         ]},
    ])


def test_fingerprints_separate_answer_sheet_from_question_page():
    sheet = compute_visual_fingerprint(_answer_sheet_image())
    question = compute_visual_fingerprint(_question_page_image())
    student_sheet = compute_visual_fingerprint(_with_handwriting(_answer_sheet_image()))
    assert fingerprint_distance(student_sheet, sheet) <= 0.25
    assert fingerprint_distance(student_sheet, question) - fingerprint_distance(student_sheet, sheet) >= 0.12


def test_role_cache_keeps_question_page_out_of_graded_baselines():
    cache = _role_cache()
    assert set(cache._baselines) == {"page_2"}
    assert cache._non_graded_pages == {"page_1": "question_only"}
    contract = {entry["page_type_id"]: entry for entry in cache._frozen_page_types_contract()}
    assert contract["page_1"]["page_role"] == "question_only"
    assert contract["page_1"]["question_numbers"] == []
    assert contract["page_1"]["printed_page_label"] == "试题卷"
    assert contract["page_2"]["page_role"] == "answer_sheet"
    assert contract["page_2"]["printed_question_labels"] == ["1.", "2."]
    assert "observed_page_label" in cache._build_page_and_answers_prompt()


def test_answer_sheet_routes_correctly_with_overlapping_printed_numbers():
    cache = _role_cache()
    student = compute_visual_fingerprint(_with_handwriting(_answer_sheet_image()))
    assert cache._resolve_joint_page_type({
        "page_type_id": "page_2", "observed_question_numbers": ["1", "2"],
        "observed_page_label": "答题卡", "observed_printed_labels": ["1", "2"],
    }, visual_fingerprint=student) == ("page_2", ["1", "2"])


@pytest.mark.parametrize("data,fingerprint_page", [
    # Printed numbers overlap, but the printed caption is the question booklet.
    ({"page_type_id": "page_2", "observed_question_numbers": ["1", "2"],
      "observed_page_label": "试题卷"}, None),
    # No caption read, but the page visually is the question booklet.
    ({"page_type_id": "page_2", "observed_question_numbers": ["1", "2"]}, "question"),
    # Printed labels the answer sheet does not carry.
    ({"page_type_id": "page_2", "observed_question_numbers": ["1"],
      "observed_printed_labels": ["一"]}, None),
    # A blank/unreadable page claimed as the question booklet without evidence.
    ({"page_type_id": "page_1", "observed_question_numbers": []}, None),
    # The model picks the booklet while its numbers point to the answer sheet.
    ({"page_type_id": "page_1", "observed_question_numbers": ["1", "2"],
      "observed_page_label": "试题卷"}, None),
])
def test_question_page_is_never_falsely_matched_to_the_answer_sheet(data, fingerprint_page):
    cache = _role_cache()
    fingerprint = (compute_visual_fingerprint(_with_handwriting(_question_page_image()))
                   if fingerprint_page == "question" else "")
    with pytest.raises(ValueError, match="需人工复核"):
        cache._resolve_joint_page_type(data, visual_fingerprint=fingerprint)


def test_student_question_page_is_reported_not_graded(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, patch

    cache = _role_cache()
    student_path = tmp_path / "student-question-page.png"
    _with_handwriting(_question_page_image()).save(student_path)
    response = json.dumps({
        "page_type_id": "page_1", "observed_question_numbers": [], "questions": [],
        "observed_page_label": "", "observed_printed_labels": ["1", "2"],
        "student_name": "", "student_no": "",
    }, ensure_ascii=False)
    with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)), patch(
        "services.ocr_service._prepare_vision_image", new=AsyncMock(return_value=str(student_path)),
    ):
        result = asyncio.run(cache._recognize_answers_only(str(student_path), auto_route=True))

    assert "error" not in result
    assert result["_cache_type_label"] == "page_1"
    assert result["_unexpected_page_type"] is True
    assert result["_not_graded_page"] is True
    assert result["questions"] == []
    assert "不计分" in result["page_type_error"]

    from services.grader import grade_paper_unified
    graded = asyncio.run(grade_paper_unified(
        {"questions": [], "unexpected_page_type": True, "page_not_graded": True,
         "page_type_error": result["page_type_error"]},
        {}, "学生", 1, category="exam",
    ))
    assert graded["page_type_status"] == "not_graded"
    assert graded["details"] == []


def test_ingest_keeps_not_graded_page_out_of_ocr_failures(monkeypatch, tmp_path):
    import routers.grade as gr_router
    from services.quick_grader import QuickBatch

    _engine, Session = _make_app_and_session()
    batch = QuickBatch("not-graded-page", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    image_path = str(tmp_path / "page.png")
    batch.uploads = [{"paper_id": "paper-1", "image_path": image_path}]
    db = Session()
    try:
        failure = gr_router._ingest_recognized_paper(batch, db, 1, "page.png", image_path, {
            "questions": [], "student_name": "", "student_no": "",
            "_unexpected_page_type": True, "_not_graded_page": True, "_page_role": "question_only",
            "page_type_error": "该页为题目页（不计分），未批改",
        })
    finally:
        db.close()
    assert failure is None
    paper = batch.papers[0]
    assert paper["unexpected_page_type"] is True
    assert paper["page_not_graded"] is True
    assert paper["page_role"] == "question_only"
    assert not paper.get("ocr_failure")


def test_import_with_teacher_roles_skips_ungraded_page_and_confirms(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    calls = []
    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _two_page_pipeline(calls))
    client = _client(Session, monkeypatch)

    rejected = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "daily",
              "page_roles": json.dumps(["question_only", "non_gradable"])},
        files=[
            ("files", ("q.png", _template_png_bytes("Q"), "image/png")),
            ("files", ("c.png", _template_png_bytes("C"), "image/png")),
        ],
    )
    assert rejected.status_code == 422

    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "daily",
              "page_roles": json.dumps(["question_only", "answer_sheet"]),
              "page_labels": json.dumps(["试题卷", "答题卡"])},
        files=[
            ("files", ("booklet.png", _template_png_bytes("BOOKLET"), "image/png")),
            ("files", ("sheet.png", _template_png_bytes("SHEET"), "image/png")),
        ],
    )
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"], events
    template_id = next(payload for event, payload in events if event == "done")["template_id"]
    assert len(calls) == 1, "an ungraded page must not spend model calls"

    sess = TemplateSession.load_meta(template_id, str(tmp_path))
    booklet, sheet = sess.pages
    assert booklet["page_role"] == "question_only" and booklet["questions"] == []
    assert booklet["page_identity"]["printed_page_label"] == "试题卷"
    assert sheet["page_role"] == "answer_sheet"
    assert [q["question_no"] for q in sheet["questions"]] == ["1", "2"]
    assert set(sess.page_artifacts) == {"page_2"}
    with pytest.raises(ValueError, match="不计分页"):
        insert_question(sess.pages, page_index=0)

    confirm = client.post("/api/batch-grade/template/confirm", json={
        "template_id": template_id, "category": "daily",
        "pages": [
            {"page_index": 0, "questions": []},
            {"page_index": 1, "questions": [
                {key: question[key] for key in (
                    "question_no", "question_text", "question_type", "standard_answer",
                    "answer_slots", "blank_count")}
                for question in sheet["questions"]
            ]},
        ],
    })
    assert confirm.status_code == 200, confirm.text
    confirmed = TemplateSession.load_meta(template_id, str(tmp_path))
    assert confirmed.recognition_contract_error() == ""
    assert confirmed.pages[0]["page_role"] == "question_only"
    assert confirmed.pages[0]["page_identity"]["printed_page_label"] == "试题卷"
    cache = gr_router._build_template_paper_cache(confirmed)
    assert set(cache._baselines) == {"page_2"}
    assert cache._non_graded_pages == {"page_1": "question_only"}


def test_old_template_without_page_role_loads_as_answer_page_and_still_confirms(monkeypatch, tmp_path):
    legacy = TemplateSession("legacy-role", str(tmp_path), teacher_id=1)
    legacy.status = "confirmed"
    legacy.category = "daily"
    legacy.add_page(0, [{"question_number": "1", "question_text": "1+1=?",
                         "question_type": "fill", "standard_answer": "2"}])
    legacy.confirm_current_contract(1)
    legacy.save_meta()
    stored = json.loads((tmp_path / "template_legacy-role" / "meta.json").read_text(encoding="utf-8"))
    assert "page_role" not in stored["pages"][0]
    confirmed_hashes = (legacy.confirmed_contract_hash, legacy.confirmed_answers_hash)

    reopened = TemplateSession.load_meta("legacy-role", str(tmp_path))
    assert page_role_of(reopened.pages[0]) == "answer_page"
    assert "page_role" not in reopened.pages[0]
    assert (reopened.content_hash(), reopened.answers_hash()) == confirmed_hashes
    assert reopened.recognition_contract_error() == ""
    assert set(answer_contract_pages(reopened.pages)[0]) == {"page_index", "questions"}
    cache = PaperCache.from_confirmed_template_pages(reopened.pages)
    assert cache._page_roles == {} and cache._non_graded_pages == {}
    assert "observed_page_label" not in cache._build_page_and_answers_prompt()

    # A present role joins the answer contract; a legacy page's does not.
    with_role = copy.deepcopy(reopened.pages)
    with_role[0]["page_role"] = "answer_page"
    assert answer_contract_pages(with_role)[0]["page_role"] == "answer_page"

    # The unconfirmed legacy draft still confirms through the endpoint.
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    draft = TemplateSession("legacy-draft", str(tmp_path), teacher_id=1)
    draft.status = "ocr_done"
    draft.add_page(0, [{"question_number": "1", "question_text": "1+1=?",
                        "question_type": "fill", "standard_answer": "2"}])
    draft.save_meta()
    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "legacy-draft", "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "1+1=?", "question_type": "fill",
            "standard_answer": "2"}]}],
    })
    assert response.status_code == 200, response.text
    saved = TemplateSession.load_meta("legacy-draft", str(tmp_path))
    assert saved.status == "confirmed"
    assert "page_role" not in saved.pages[0]
    assert page_role_of(saved.pages[0]) == "answer_page"
    assert saved.recognition_contract_error() == ""


def test_ungraded_page_cannot_carry_questions():
    with pytest.raises(ValueError, match="不计分页"):
        TemplateSession.make_page(0, [{"question_no": "1"}], page_role="question_only")
    with pytest.raises(ValueError):
        TemplateSession.make_page(0, [{"question_no": "1"}], page_role="cover")
    with pytest.raises(ValueError, match="not graded"):
        PaperCache.from_confirmed_template_pages([
            {"page_index": 0, "page_role": "non_gradable", "questions": [{"question_no": "1"}]},
            {"page_index": 1, "questions": [{"question_no": "2"}]},
        ])
    with pytest.raises(ValueError, match="no graded pages"):
        PaperCache.from_confirmed_template_pages([
            {"page_index": 0, "page_role": "question_only", "questions": []},
        ])
