"""A question the page break cut in two is solved and transcribed with the tail of the page before it.

The measured case (2024 college entrance exam, page 4, 2026-11-11): the page
opens with a figure and two sub-questions of question 17 whose stem is at the
bottom of page 3. Seeing only page 4, one run solved it from the real
conditions and the next made a set of conditions up. The import knows the page
before; the request that owns the question now also sees where it began.
"""
import asyncio
import copy
import json
import logging
from pathlib import Path

import pytest
from PIL import Image

import config
from routers import grade_recognition
from routers import grade_template_import
from services import template_cross_page as cross_page
from services import template_directory_pipeline as pipeline
from services import template_max_owned as owned
from services import template_one_three as contract
from services.teaching_context import make_teaching_context, use_teaching_context
from tests.grade_family import set_grade_global
from tests.test_template_flash_page_numbering import CONTINUED_PAGE, SOLVE_PAGE_BY_POSITION, coarse_wire
from tests.test_template_one_three import v4_payloads
from tests.test_template_session import (  # noqa: F401 - autouse readiness fixture
    _client,
    _make_app_and_session,
    _parse_sse,
    _patch_template_layout,
    _ready_batch_grade_chain,
    _template_png_bytes,
)

SOLVE, TRANSCRIBE = contract.SOLVE_PHASE, contract.TRANSCRIBE_PHASE
TAIL_NAME = "previous-page-tail.png"
# The previous page is 300 x 400; its last question's printed number sits at 70% of the height.
PREVIOUS_SIZE = (300, 400)
LAST_NUMBER = [60, 700, 100, 730]
TAIL = [0, 274, 300, 400]


def painted(path, size=PREVIOUS_SIZE) -> str:
    """An image whose every pixel names its own position, so a crop shows where it was cut."""
    width, height = size
    data = bytes(channel for y in range(height) for x in range(width)
                 for channel in (x % 256, y % 256, (x // 256) * 16 + y // 256))
    Image.frombytes("RGB", size, data).save(path, format="PNG")
    return str(path)


def cut_of(image_path) -> list[int]:
    """The pixel box of the painted page that this image (the page or a crop of it) shows."""
    with Image.open(image_path) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
        left, top = red + 256 * (blue // 16), green + 256 * (blue % 16)
        return [left, top, left + image.width, top + image.height]


def size_of(image_path) -> tuple[int, int]:
    with Image.open(image_path) as image:
        return image.size


def pixels_of(image_path) -> bytes:
    with Image.open(image_path) as image:
        return image.convert("RGB").tobytes()


def recognized(number_box=LAST_NUMBER, columns=None) -> dict:
    """The result of a recognized previous page: two questions, the last one's number at ``number_box``."""
    return {"question_anchors": [{"question_number": "1", "question_number_bbox": [60, 100, 100, 130]},
                                 {"question_number": "2", "question_number_bbox": number_box}],
            "layout_result": {"column_layout": {"columns": columns}} if columns else {}}


def previous_page(tmp_path, *, result="recognized", role="answer_page") -> cross_page.PreviousPage:
    return cross_page.PreviousPage(role, painted(tmp_path / "previous.png"),
                                   recognized() if result == "recognized" else result)


def recognize(tmp_path, monkeypatch, hierarchy_wire, previous=None, *, shards=2, role="answer_page",
              unsure=None):
    """The real pipeline with scripted models; returns the result and every request by its stage key.

    ``unsure`` is what the solver writes into ``uncertain`` for the first question.
    """
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", shards)
    monkeypatch.setattr(config, "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", 4)
    monkeypatch.setattr(config, "OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", 0)
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    _, branches = v4_payloads(tuple(len(item["slots"]) for item in hierarchy_wire["questions"]))
    if contract.continued_question(contract.receive_hierarchy(copy.deepcopy(hierarchy_wire), version=4)):
        branches["template-directory-v1"]["questions"][0]["question_number_bbox"] = None
    branches[SOLVE]["answers"][0]["uncertain"] = list(unsure or [])
    calls = {}

    async def request(images, prompt, phase, validator, **options):
        paths = [images] if isinstance(images, str) else list(images)
        key = options.get("stage_key") or phase
        assert key not in calls
        # The crops live in a directory the pipeline removes: read them while the request runs.
        calls[key] = {"prompt": prompt, "paths": paths,
                      "images": [(Path(path).name, size_of(path)) for path in paths],
                      "last_cut": cut_of(paths[-1]) if previous and Path(paths[-1]).name == TAIL_NAME else None,
                      "options": {name: value for name, value in options.items() if not callable(value)}}
        if phase == "template-hierarchy-v1":
            return validator(copy.deepcopy(hierarchy_wire))
        if phase == "template-coarse-locator-v1":
            return validator(coarse_wire([str(index) for index in range(1, len(hierarchy_wire["questions"]) + 1)]))
        return validator(contract.parse_json(json.dumps(branches[phase], ensure_ascii=False)))

    async def run():
        with use_teaching_context(make_teaching_context("senior")), cross_page.after(previous, role=role):
            return await pipeline.recognize_directory_template(
                str(source), template_mode="blank", layout_result={})

    monkeypatch.setattr(pipeline, "_request", request)
    return asyncio.run(run()), calls


def sent(call) -> tuple:
    """What a request put on the wire, without the paths of its temporary crops."""
    return call["prompt"], call["images"], call["options"]


def row_of(prompt, q_id="Q1") -> dict:
    """The directory row a prompt carries for one question."""
    directory, _ = json.JSONDecoder().raw_decode(prompt.split("\n目录：", 1)[1])
    return next(row for row in directory if row["q_id"] == q_id)


def manifest_of(hierarchy_wire) -> list[dict]:
    return contract.question_contract(contract.receive_hierarchy(copy.deepcopy(hierarchy_wire), version=4))


@pytest.mark.parametrize("shards, owner", [
    pytest.param(1, SOLVE, id="one_solve_request"),
    pytest.param(2, SOLVE + "#1", id="two_shards"),
    pytest.param(3, SOLVE + "#1", id="one_shard_per_question"),
])
def test_only_the_solver_request_that_owns_the_carried_over_question_and_the_transcription_see_the_previous_pages_tail(
        tmp_path, monkeypatch, shards, owner):
    alone, before = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, shards=shards)
    result, calls = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, previous_page(tmp_path), shards=shards)

    assert "error" not in alone and "error" not in result
    assert set(calls) == set(before) and len([key for key in calls if key.startswith(SOLVE)]) == shards
    # Every other request - both Flash hints, the locator, the other shards - is unchanged.
    bound = {owner, TRANSCRIBE}
    assert {key: sent(call) for key, call in calls.items() if key not in bound} == {
        key: sent(call) for key, call in before.items() if key not in bound}
    assert len(calls) - len(bound) == 2 + 1 + shards - 1
    # Both keep their page and crops, in order; the tail comes last, cut from the last question's number down.
    for key in bound:
        assert calls[key]["images"] == before[key]["images"] + [(TAIL_NAME, (300, 126))]
        assert calls[key]["last_cut"] == TAIL
        assert calls[key]["options"] == before[key]["options"]
        assert "Q1" in calls[key]["options"]["expected_ids"]
    assert result["template_ocr_diagnostics"]["carried_over_question"] == {
        "view": "上一页的尾部", "box_px": TAIL, "requests": sorted(bound)}
    assert "carried_over_question" not in alone["template_ocr_diagnostics"]


def test_the_owners_directory_row_says_where_the_stem_is_and_no_longer_says_this_page_only(tmp_path, monkeypatch):
    _, before = recognize(tmp_path, monkeypatch, CONTINUED_PAGE)
    _, calls = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, previous_page(tmp_path))
    old, new = before[SOLVE + "#1"]["prompt"], calls[SOLVE + "#1"]["prompt"]

    assert row_of(old)["note"] == owned.CONTINUED_NOTE and owned.CONTINUED_NOTE not in new
    note = row_of(new)["note"]
    assert "最后一张图是上一页的尾部" in note and "那张图中的最后一道题" in note
    assert "两处都没有的已知条件不得自行补设" in note and "那张图中的其他题目不属于本请求" in note
    assert "只依据本页可见内容" not in new and "question_number_bbox返回null" not in new
    # Nothing but that note differs: it is the prompt built from a directory whose row carries it.
    told = manifest_of(CONTINUED_PAGE)
    told[0]["note"] = note
    rebuilt, _ = owned.build_prompts(told, education_stage="senior")
    assert new.startswith(rebuilt) and new[len(rebuilt):] == old[len(rebuilt) - len(note) + len(owned.CONTINUED_NOTE):]


def test_the_transcriptions_directory_row_says_to_join_the_stem_with_this_pages_text(tmp_path, monkeypatch):
    _, before = recognize(tmp_path, monkeypatch, CONTINUED_PAGE)
    _, calls = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, previous_page(tmp_path))
    old, new = before[TRANSCRIBE]["prompt"], calls[TRANSCRIBE]["prompt"]

    assert row_of(old)["note"] == owned.CONTINUED_NOTE and owned.CONTINUED_NOTE not in new
    note = row_of(new)["note"]
    assert "最后一张图是上一页的尾部" in note and "那张图中的最后一道题" in note
    assert "先录那里的本题题干，再接本页上本题的正文" in note and "不转录" in note
    # It is told how to transcribe, not how to answer; the solver's row is the other way round.
    assert "answer留空" not in note and "转录" not in row_of(calls[SOLVE + "#1"]["prompt"])["note"]
    told = manifest_of(CONTINUED_PAGE)
    told[0]["note"] = note
    _, rebuilt = owned.build_prompts(told, education_stage="senior")
    assert new.startswith(rebuilt) and new[len(rebuilt):] == old[len(rebuilt) - len(note) + len(owned.CONTINUED_NOTE):]


def test_the_teacher_is_told_the_answer_came_from_both_pages_and_what_the_solver_was_unsure_of(tmp_path, monkeypatch):
    alone, _ = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, unsure=["PA的长度看不清"])
    result, _ = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, previous_page(tmp_path), unsure=["PA的长度看不清"])

    first, *rest = result["questions"]
    assert first["answer_status"] == "needs_review" and result["requires_teacher_confirmation"] == ["1"]
    assert [reason for reason in first["directory_issues"] if "接上页" in reason or "看不清" in reason] == [
        cross_page.BOUND_REVIEW_REASON, "PA的长度看不清"]
    assert owned.CONTINUED_REVIEW_REASON not in first["directory_issues"]
    assert [question["answer_status"] for question in rest] == ["ready", "ready"]
    # Without the previous page the reason still says the answer came from this page only.
    assert owned.CONTINUED_REVIEW_REASON in alone["questions"][0]["directory_issues"]
    assert cross_page.BOUND_REVIEW_REASON not in alone["questions"][0]["directory_issues"]
    assert "上一页的题干与本页内容共同得出" in cross_page.BOUND_REVIEW_REASON


def test_a_page_without_a_carried_over_question_sends_what_it_sent_without_a_previous_page(tmp_path, monkeypatch):
    alone, before = recognize(tmp_path, monkeypatch, SOLVE_PAGE_BY_POSITION)
    result, calls = recognize(tmp_path, monkeypatch, SOLVE_PAGE_BY_POSITION, previous_page(tmp_path))

    assert "error" not in result and result["requires_teacher_confirmation"] == []
    assert {key: sent(call) for key, call in calls.items()} == {key: sent(call) for key, call in before.items()}
    assert result["questions"] == alone["questions"]
    assert "carried_over_question" not in result["template_ocr_diagnostics"]


def test_a_previous_page_that_was_not_recognized_is_shown_whole(tmp_path, monkeypatch):
    previous = previous_page(tmp_path, result=None, role="question_only")
    _, before = recognize(tmp_path, monkeypatch, CONTINUED_PAGE)
    result, calls = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, previous)
    owner = calls[SOLVE + "#1"]

    assert owner["paths"][-1] == previous.image_path and owner["paths"][:-1] == calls[SOLVE + "#2"]["paths"]
    assert owner["images"] == before[SOLVE + "#1"]["images"] + [("previous.png", PREVIOUS_SIZE)]
    assert calls[TRANSCRIBE]["paths"] == owner["paths"]
    assert all("最后一张图是上一页整页" in row_of(calls[key]["prompt"])["note"] for key in (SOLVE + "#1", TRANSCRIBE))
    assert result["template_ocr_diagnostics"]["carried_over_question"] == {
        "view": "上一页整页", "requests": [TRANSCRIBE, SOLVE + "#1"]}


def test_a_previous_page_that_cannot_be_read_leaves_the_page_as_it_was(tmp_path, monkeypatch, caplog):
    missing = cross_page.PreviousPage("answer_page", str(tmp_path / "gone.png"), recognized())
    alone, before = recognize(tmp_path, monkeypatch, CONTINUED_PAGE)
    with caplog.at_level(logging.WARNING, logger="services.template_cross_page"):
        result, calls = recognize(tmp_path, monkeypatch, CONTINUED_PAGE, missing)

    assert "error" not in result and result["questions"] == alone["questions"]
    assert {key: sent(call) for key, call in calls.items()} == {key: sent(call) for key, call in before.items()}
    assert "carried_over_question" not in result["template_ocr_diagnostics"]
    assert any("上一页的图无法读取" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("result, box", [
    pytest.param(recognized(), TAIL, id="single_column"),
    pytest.param(recognized([560, 700, 600, 730], [{"x1_1000": 0, "x2_1000": 500}, {"x1_1000": 500, "x2_1000": 1000}]),
                 [145, 274, 300, 400], id="last_question_in_the_last_column"),
    pytest.param(recognized([560, 700, 600, 730], [{"x1_1000": 500, "x2_1000": 1000}, {"x1_1000": 0, "x2_1000": 500}]),
                 [145, 274, 300, 400], id="columns_listed_right_to_left"),
    pytest.param(recognized([60, 10, 100, 40]), [0, 0, 300, 400], id="number_at_the_top_keeps_the_page_height"),
    pytest.param(recognized([60, 980, 100, 995]), [0, 360, 300, 400], id="never_a_sliver"),
    pytest.param(recognized([60, 700, 100, 730], [{"x1_1000": 0, "x2_1000": 500}, {"x1_1000": 500, "x2_1000": 1000}]),
                 None, id="last_question_not_in_the_last_column"),
    pytest.param(None, None, id="page_was_not_recognized"),
    pytest.param({"question_anchors": []}, None, id="no_questions"),
    pytest.param({"question_anchors": [{"question_number": "1"}]}, None, id="last_question_has_no_number_box"),
    pytest.param(recognized(None), None, id="number_box_is_null"),
    pytest.param(recognized([60, 730, 100, 700]), None, id="inverted_box"),
    pytest.param(recognized([60, 700, 100, 1200]), None, id="box_outside_the_page"),
    pytest.param(recognized([60, 700, "x", 730]), None, id="box_is_not_numbers"),
    pytest.param(recognized([60, 700, 100]), None, id="box_is_not_four_numbers"),
    pytest.param(recognized(LAST_NUMBER, [{"x1_1000": 0}]), None, id="column_without_a_right_edge"),
    pytest.param(recognized(LAST_NUMBER, ["column"]), None, id="column_is_not_a_mapping"),
])
def test_the_tail_runs_from_the_last_questions_number_to_the_bottom_of_its_column(result, box):
    previous = cross_page.PreviousPage("answer_page", "unused.png", result)

    assert cross_page.tail_box(previous, width=300, height=400) == box


def bound_request(tmp_path, manifest, previous, *, role="answer_page"):
    """``bind`` under ``after``; returns the request, its report and what reached the transport."""
    wire = []

    async def request(images, prompt, phase, validator, **options):
        wire.append({"images": images, "prompt": prompt, "phase": phase, "validator": validator, "options": options})

    with cross_page.after(previous, role=role):
        ask, report = cross_page.bind(request, manifest, tmp_path)
    return request, ask, report, wire


@pytest.mark.parametrize("role, previous_role, bound", [
    ("answer_page", "answer_page", True),
    ("answer_page", "question_only", True),
    ("answer_page", "non_gradable", False),    # a cover prints no question
    ("answer_page", "answer_sheet", False),    # an answer sheet prints no stem
    ("answer_sheet", "answer_page", False),    # the stems of an answer sheet are in a booklet, not on the page before
    ("answer_sheet", "question_only", False),
])
def test_a_stem_is_only_looked_for_between_pages_that_print_questions(tmp_path, role, previous_role, bound):
    previous = previous_page(tmp_path, role=previous_role)
    request, ask, report, _ = bound_request(tmp_path, manifest_of(CONTINUED_PAGE), previous, role=role)

    assert (ask is not request) is bound and bool(report) is bound


def test_the_request_itself_comes_back_when_there_is_nothing_to_bind(tmp_path):
    previous = previous_page(tmp_path)
    for manifest, page_before in ((manifest_of(SOLVE_PAGE_BY_POSITION), previous),   # no carried-over question
                                  (manifest_of(CONTINUED_PAGE), None)):              # the import named no page
        request, ask, report, _ = bound_request(tmp_path, manifest, page_before)
        assert ask is request and report == {}
    # The page before is known only while its ``after`` block is open.
    request, _, _, _ = bound_request(tmp_path, manifest_of(CONTINUED_PAGE), previous)
    assert cross_page.bind(request, manifest_of(CONTINUED_PAGE), tmp_path) == (request, {})


@pytest.mark.parametrize("phase, expected_ids, prompt", [
    pytest.param(SOLVE, ("Q2", "Q3"), owned.CONTINUED_NOTE, id="a_shard_that_does_not_own_the_question"),
    pytest.param("template-directory-v1", ("Q1",), owned.CONTINUED_NOTE, id="the_locator"),
    pytest.param("template-hierarchy-v1", ("Q1",), owned.CONTINUED_NOTE, id="a_flash_hint_request"),
    pytest.param(SOLVE, ("Q1",), "目录里没有那句说明", id="a_prompt_without_the_row_note"),
    pytest.param(TRANSCRIBE, ("Q1", "Q2", "Q3"), "目录里没有那句说明", id="a_transcription_without_the_row_note"),
    pytest.param(SOLVE, ("Q1",), owned.CONTINUED_NOTE * 2, id="a_prompt_that_carries_the_note_twice"),
])
def test_a_request_the_tail_is_not_meant_for_passes_through_untouched(tmp_path, phase, expected_ids, prompt):
    request, ask, report, wire = bound_request(tmp_path, manifest_of(CONTINUED_PAGE), previous_page(tmp_path))
    images, validator = ["page.png", "crop.png"], object()

    asyncio.run(ask(images, prompt, phase, validator, expected_ids=expected_ids, stage_key="k"))

    assert ask is not request and report["requests"] == []
    assert wire == [{"images": images, "prompt": prompt, "phase": phase, "validator": validator,
                     "options": {"expected_ids": expected_ids, "stage_key": "k"}}]
    assert wire[0]["images"] is images


def test_one_page_image_given_as_a_path_is_followed_by_the_tail(tmp_path):
    _, ask, report, wire = bound_request(tmp_path, manifest_of(CONTINUED_PAGE), previous_page(tmp_path))

    asyncio.run(ask("page.png", owned.CONTINUED_NOTE, SOLVE, lambda payload: payload, expected_ids=("Q1",)))

    assert wire[0]["images"] == ["page.png", str(tmp_path / TAIL_NAME)] and report["requests"] == [SOLVE]


def test_the_transcription_carries_the_tail_and_keeps_its_own_validator(tmp_path):
    _, ask, report, wire = bound_request(tmp_path, manifest_of(CONTINUED_PAGE), previous_page(tmp_path))
    validator = object()

    asyncio.run(ask(["page.png"], owned.CONTINUED_NOTE, TRANSCRIBE, validator, expected_ids=("Q1", "Q2", "Q3")))

    assert wire[0]["images"] == ["page.png", str(tmp_path / TAIL_NAME)] and report["requests"] == [TRANSCRIBE]
    # Review reasons belong to the solver's answers; a transcription has none to rewrite.
    assert wire[0]["validator"] is validator


def _carried_over_pipeline(calls, tmp_path):
    """A page pipeline that asks, like the real one, for the request of its carried-over question."""
    row = manifest_of(CONTINUED_PAGE)[:1]

    async def fake_pipeline(path, **_kwargs):
        shown = []

        async def request(images, prompt, phase, validator, **options):
            shown.extend((image, pixels_of(image)) for image in images)

        ask, report = cross_page.bind(request, row, tmp_path)
        await ask([path], owned.CONTINUED_NOTE, SOLVE, None, expected_ids=("Q1",))
        calls.append({"page": path, "shown": shown, "report": report})
        questions = [{"question_number": number, "question_no": number, "q_id": "Q" + number,
                      "question_text": f"第{number}题", "question_type": "fill", "standard_answer": number,
                      "answer_slots": [number], "slot_ids": [f"{number}[1]"], "printed_path": [f"{number}."],
                      "directory_node_id": f"n00{number}", "directory_parent_id": None} for number in ("1", "2")]
        tops = {"1": 220, "2": 320}
        return {"questions": questions,
                "template_answer_regions": [{"question_number": number, "regions": [[500, top, 850, top + 60]],
                                             "slot_ids": [f"{number}[1]"]} for number, top in tops.items()],
                "question_anchors": [{"question_number": number, "question_number_bbox": [60, top, 100, top + 30]}
                                     for number, top in tops.items()]}

    return fake_pipeline


def _page_png(index: int) -> bytes:
    """An uploadable page whose lower half carries a mark at a place of its own."""
    import io
    from PIL import ImageDraw

    image = Image.open(io.BytesIO(_template_png_bytes(f"P{index}")))
    ImageDraw.Draw(image).rectangle((80 + 70 * index, 690, 120 + 70 * index, 730), fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _import_pages(monkeypatch, tmp_path, roles):
    """Import one page per role through the real route; returns what each recognized page was shown."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    calls = []
    crops = tmp_path / "crops"
    crops.mkdir(exist_ok=True)
    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _carried_over_pipeline(calls, crops))
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "daily", "page_roles": json.dumps(roles)},
        files=[("files", (f"page-{index}.png", _page_png(index), "image/png"))
               for index in range(1, len(roles) + 1)])
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"], events
    return calls


def test_the_import_hands_each_page_the_page_uploaded_before_it(monkeypatch, tmp_path):
    first, second, third = _import_pages(monkeypatch, tmp_path, ["answer_page"] * 3)

    # The first page has no page before it; each later page is shown the tail of the one just recognized.
    assert first["report"] == {} and [path for path, _ in first["shown"]] == [first["page"]]
    for page, before in ((second, first), (third, second)):
        with Image.open(before["page"]) as rectified:
            width, height = rectified.size
            box = [0, (320 - 15) * height // 1000, width, height]
            tail = rectified.convert("RGB").crop(tuple(box)).tobytes()
        assert page["report"] == {"view": "上一页的尾部", "box_px": box, "requests": [SOLVE]}
        assert [path for path, _ in page["shown"]][0] == page["page"] and page["shown"][-1][1] == tail
    # Each tail is the page just before, not the first page and not the page itself.
    assert second["shown"][-1][1] != third["shown"][-1][1]
    assert len({first["page"], second["page"], third["page"]}) == 3


def test_the_import_shows_a_question_only_page_whole_and_skips_a_cover(monkeypatch, tmp_path):
    after_questions, = _import_pages(monkeypatch, tmp_path, ["question_only", "answer_page"])
    after_cover, = _import_pages(monkeypatch, tmp_path, ["non_gradable", "answer_page"])

    # A question-only page is never recognized: it has no number box to cut at, so its rectified image goes whole.
    assert after_questions["report"] == {"view": "上一页整页", "requests": [SOLVE]}
    shown = [path for path, _ in after_questions["shown"]]
    assert shown[0] == after_questions["page"] and len(shown) == 2
    # The other rectified page of the same template, stored before this one.
    own, other = Path(shown[0]), Path(shown[1])
    assert other.name == own.name and other.parent.parent == own.parent.parent
    assert other.parent.name < own.parent.name and other.is_file()
    assert after_cover["report"] == {} and [path for path, _ in after_cover["shown"]] == [after_cover["page"]]
