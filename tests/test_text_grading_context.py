"""Whole confirmed references and sparse student work are separate inputs."""
import asyncio
import copy
import json

import pytest

from services import analyzer, flash_grading as flash, grader
from services.grading_contract import AnswerSpec
from services.scoring_engine import build_grading_units, llm_payload_for_unit


def spec(parent, qtype, ids):
    answers = [f"参考-{slot}" for slot in ids]
    return AnswerSpec(
        question_no=parent, question_type=qtype,
        question_text=f"完整题干-{parent}：保留全部条件、单位和前后小问。\n后一问可用前一问的条件。",
        standard_answer="；".join(answers), answer_slots=answers, slot_ids=ids,
        blank_count=len(ids), subquestion_count=len(ids),
        region_kinds=["answer"] * len(ids), solution_steps=[f"公共参考步骤-{parent}"],
        answer_variants=[f"教师确认的其他解法-{parent}"],
        grading_method="process" if qtype == "solve" else "equivalent",
    )


def fixture_data(qtype="fill"):
    specs = [spec("2", qtype, ["2[1]", "2[8]"]),
             spec("7", "solve" if qtype == "fill" else "fill", ["7[3]"]),
             spec("9", "solve", ["9[1]"])]
    answer_map = {s.question_no: s.to_answer_map_entry() for s in specs}
    paper = {"paper_id": "synthetic-context", "requires_separate_grading": True, "questions": [
        {"question_no": "2", "question_text": "OCR正文不得覆盖教师题干", "student_slots": [
            {"slot_id": "2[8]", "student_answer": "原始转录-乙\n过程未改写", "answer_state": "visible"},
            {"slot_id": "2[1]", "student_answer": "", "answer_state": "blank"}]},
        {"question_no": "7", "student_slots": [
            {"slot_id": "7[3]", "student_answer": "[无法识别]", "answer_state": "unreadable"}]},
    ]}
    return specs, answer_map, paper


class ImmediateQueue:
    async def submit(self, coro_factory, **kwargs):
        return await coro_factory()


def verdict(detail="detailed"):
    row = {"result": "correct"}
    if detail == "detailed":
        row.update(explanation="", hint="", model_answer="", standard_answer="")
    return row


def stub_transport(monkeypatch, route, captured, detail="detailed"):
    async def transport(messages, **kwargs):
        captured.append((copy.deepcopy(messages), kwargs))
        answers = json.loads(messages[1]["content"])["answers"]
        ids = [a["question_no"] for a in answers]
        content = json.dumps({"results": {qn: verdict(detail) for qn in reversed(ids)}})
        kwargs["response_validator"](content)
        return {"content": content, "receipt": {}, "model": flash.FLASH_MODEL,
                "parameters": {}, "usage": {}}

    monkeypatch.setattr(grader, "get_grading_queue", ImmediateQueue)
    monkeypatch.setattr(grader, "judge_paper", analyzer.judge_paper)
    monkeypatch.setattr(flash, "call_flash", transport)


@pytest.mark.parametrize("route", ["flash"])
@pytest.mark.parametrize("qtype", ["fill", "solve"])
@pytest.mark.parametrize("detail", ["simple", "detailed"])
def test_confirmed_stems_and_answers_survive_reopen_to_actual_wire(monkeypatch, tmp_path, route, qtype, detail):
    specs, answer_map, paper = fixture_data(qtype)
    saved = tmp_path / "confirmed.json"
    saved.write_text(json.dumps([s.model_dump() for s in specs], ensure_ascii=False), encoding="utf-8")
    reopened = [AnswerSpec.model_validate(v) for v in json.loads(saved.read_text(encoding="utf-8"))]
    answer_map = {s.question_no: s.to_answer_map_entry() for s in reopened}
    before = copy.deepcopy((answer_map, paper))
    captured = []
    stub_transport(monkeypatch, route, captured, detail)
    result = asyncio.run(grader.grade_paper_unified(paper, answer_map, "synthetic", 1, detail_level=detail))
    assert len(captured) == 1
    messages, options = captured[0]
    system = messages[0]["content"]
    refs = json.loads(system.split("教师确认资料：", 1)[1])
    assert [r["question_no"] for r in refs] == ["2", "7", "9"]
    for reference in refs:
        entry = answer_map[reference["question_no"]]
        assert reference["question_text"] == entry["question_text"]
        assert reference["standard_answer"] == entry["standard_answer"]
        assert reference["solution_steps"] == entry["solution_steps"]
        assert reference["answer_variants"] == entry["answer_variants"]
        assert reference["grading_method"] == entry["grading_method"]
        assert system.count(entry["question_text"].splitlines()[0]) == 1
        assert list(reference["answers"]) == entry["slot_ids"]
        assert [r["standard_answer"] for r in reference["answers"].values()] == entry["answer_slots"]
    assert "原始转录-乙" not in system
    assert "OCR正文不得覆盖教师题干" not in system
    assert "[无法识别]" not in system
    assert json.loads(messages[1]["content"]) == {"answers": [
        {"question_no": "2[8]", "student_answer": "原始转录-乙\n过程未改写"}]}
    if route == "flash":
        assert options["response_schema"]["properties"]["results"]["required"] == ["2[8]"]
    assert result["needs_review_count"] == 1
    if qtype == "fill":
        details = result["details"][0]["blank_results"]
    else:
        details = result["details"][:2]
        assert [d["question_no"] for d in details] == ["2(1)", "2(8)"]
    assert [d["slot_id"] for d in details] == ["2[1]", "2[8]"]
    assert [d["result"] for d in details] == ["wrong", "correct"]
    assert (answer_map, paper) == before


@pytest.mark.parametrize("route", ["flash"])
def test_student_subset_does_not_change_full_teacher_prefix(monkeypatch, route):
    _, answer_map, paper = fixture_data()
    second = copy.deepcopy(paper)
    second["questions"][0]["student_slots"][1].update(student_answer="原始转录-甲", answer_state="visible")
    captured = []
    stub_transport(monkeypatch, route, captured)
    for student in (paper, second):
        asyncio.run(grader.grade_paper_unified(student, answer_map, "synthetic", 1))
    assert captured[0][0][0] == captured[1][0][0]
    assert captured[0][0][1] != captured[1][0][1]


def test_model_parameters_participate_in_queue_identity(monkeypatch):
    _, answer_map, paper = fixture_data()
    keys = []
    stub_transport(monkeypatch, "flash", [])

    class CaptureQueue:
        async def submit(self, coro_factory, **kwargs):
            keys.append(kwargs["idempotency_key"])
            return await coro_factory()

    monkeypatch.setattr(grader, "get_grading_queue", CaptureQueue)
    for effort in ("medium", "xhigh"):
        monkeypatch.setattr(flash, "FLASH_TEXT_REASONING_EFFORT", effort, raising=False)
        asyncio.run(grader.grade_paper_unified(paper, answer_map, "synthetic", 1))
    assert keys[0] != keys[1]


def test_context_changes_participate_in_queue_identity(monkeypatch):
    _, answer_map, paper = fixture_data()
    keys = []
    captured = []
    stub_transport(monkeypatch, "flash", captured)

    class CaptureQueue:
        async def submit(self, coro_factory, **kwargs):
            keys.append(kwargs["idempotency_key"])
            return await coro_factory()

    monkeypatch.setattr(grader, "get_grading_queue", CaptureQueue)
    for field, value in [(None, None), ("question_text", "背景题干已由教师修改"),
                         ("solution_steps", ["背景题参考步骤已由教师修改"]),
                         ("answer_slots", ["背景题参考答案已由教师修改"])]:
        updated = copy.deepcopy(answer_map)
        if field:
            updated["9"][field] = value
        asyncio.run(grader.grade_paper_unified(paper, updated, "synthetic", 1))
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("route", ["flash"])
@pytest.mark.parametrize("fault", ["extra", "missing", "alias", "duplicate"])
def test_full_reference_catalog_cannot_expand_or_alias_output(monkeypatch, route, fault):
    _, answer_map, paper = fixture_data()
    captured = []
    stub_transport(monkeypatch, route, captured)

    async def invalid(messages, **kwargs):
        row = verdict()
        if fault == "duplicate":
            item = json.dumps(row)
            content = '{"results":{"2[8]":' + item + ',"2[8]":' + item + '}}'
        else:
            ids = {"extra": ["2[8]", "9[1]"], "missing": [], "alias": ["2"]}[fault]
            content = json.dumps({"results": {qn: row for qn in ids}})
        kwargs["response_validator"](content)
        raise AssertionError("invalid output must be rejected")

    monkeypatch.setattr(flash, "call_flash", invalid)
    with pytest.raises(grader.GradingModelUnavailable):
        asyncio.run(grader.grade_paper_unified(paper, answer_map, "synthetic", 1))


def test_legacy_units_keep_old_ids_and_full_parent_context():
    from services.text_grading_context import build_teacher_material
    answer_map = {"5.1": {"question_type": "solve", "question_text": "完整旧题干",
                          "standard_answer": "甲；乙", "answer_slots": ["甲", "乙"],
                          "subquestion_count": 2, "solution_steps": ["共享旧步骤"]}}
    units = build_grading_units([{"question_no": "5.1", "student_answer": "甲；乙"}], answer_map, "daily")
    material = build_teacher_material(answer_map, units)
    assert material[0]["question_text"] == "完整旧题干"
    assert material[0]["solution_steps"] == ["共享旧步骤"]
    assert list(material[0]["answers"]) == [llm_payload_for_unit(u)["question_no"] for u in units]
    assert list(material[0]["answers"]) == ["5.1(1)", "5.1(2)"]


def test_text_material_never_copies_assets_or_student_fields():
    from services.text_grading_context import build_teacher_material
    _, answer_map, paper = fixture_data()
    answer_map["2"].update(student_answer="不得复制的学生文字", image_path="private.png", token="secret-marker")
    units = build_grading_units(paper["questions"], answer_map, "daily")
    text = json.dumps(build_teacher_material(answer_map, units), ensure_ascii=False)
    assert all(marker not in text for marker in ("不得复制的学生文字", "private.png", "secret-marker", "原始转录"))


@pytest.mark.parametrize("fault", ["missing", "duplicate", "mismatched_reference", "wrong_parent"])
def test_teacher_material_identity_errors_fail_before_transport(fault):
    from services.text_grading_context import build_teacher_material, validate_teacher_material
    _, answer_map, paper = fixture_data()
    units = build_grading_units(paper["questions"], answer_map, "daily")
    material = build_teacher_material(answer_map, units)
    pending = [llm_payload_for_unit(u) for u in units if u.get("slot_id") == "2[8]"]
    if fault == "missing":
        del material[0]["answers"]["2[8]"]
    elif fault == "duplicate":
        material.append(copy.deepcopy(material[0]))
    elif fault == "wrong_parent":
        material[0]["question_no"] = "10"
    else:
        material[0]["answers"]["2[8]"]["standard_answer"] = "来自另一作答位的答案"
    with pytest.raises(ValueError):
        validate_teacher_material(material, pending)


@pytest.mark.parametrize("route", ["flash"])
def test_mixed_reference_keeps_full_context_but_only_grades_text(monkeypatch, route):
    from services.text_grading_context import build_teacher_material
    entry = {"question_type": "solve", "answer_kind": "mixed", "question_text": "完整作图与计算题干",
             "standard_answer": "文字答案与作图要求", "solution_steps": ["完整几何条件及计算步骤"],
             "slot_ids": ["3[2]", "3[5]", "3[8]"], "answer_slots": ["甲", "作图", "乙"],
             "region_kinds": ["answer", "drawing", "answer"]}
    unit = {"key": "3", "parent_question_no": "3", "question_type": "solve",
            "question_text": entry["question_text"], "standard_answer": entry["standard_answer"],
            "student_answer": "原始文字与计算"}
    material = build_teacher_material({"3": entry}, [unit])
    pending = [{**llm_payload_for_unit(unit), "scope_hint": "text_only"}]
    captured = []
    stub_transport(monkeypatch, route, captured)
    result = asyncio.run(analyzer.judge_paper(pending, teacher_material=material))
    assert list(result) == ["3"]
    refs = json.loads(captured[0][0][0]["content"].split("教师确认资料：", 1)[1])
    assert refs[0]["question_text"] == entry["question_text"]
    assert refs[0]["solution_steps"] == entry["solution_steps"]
    assert refs[0]["answers"]["3"]["scope_hint"] == "text_only"
    assert refs[0]["answers"]["3[5]"]["scope_hint"] == "drawing"
    assert json.loads(captured[0][0][1]["content"])["answers"] == [
        {"question_no": "3", "student_answer": "原始文字与计算"}]


@pytest.mark.parametrize("route", ["flash"])
def test_full_teacher_catalog_never_creates_a_call_without_pending_work(monkeypatch, route):
    _, answer_map, paper = fixture_data()
    for question in paper["questions"]:
        for slot in question["student_slots"]:
            slot.update(student_answer="", answer_state="blank")
    captured = []
    stub_transport(monkeypatch, route, captured)
    result = asyncio.run(grader.grade_paper_unified(paper, answer_map, "synthetic", 1))
    assert captured == []
    assert result["needs_review_count"] == 0


@pytest.mark.parametrize("changed_input", ["background", "effort"])
def test_frozen_context_survives_review_and_creates_new_archive(monkeypatch, tmp_path, changed_input):
    """Real grading -> review JSON -> archive -> DB; no student data or API calls."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    from models import GradingResult, Student
    from models_user import User
    from routers import grade as route
    from routers import grade_review
    from services import grading_persistence as gp
    from services.quick_grader import QuickBatch

    _, answer_map, paper = fixture_data()
    paper["questions"] = paper["questions"][:1]
    batch = QuickBatch("synthetic-context-archive", str(tmp_path))
    batch.answer_map = answer_map
    monkeypatch.setattr(gp, "ARCHIVE_DIR", str(tmp_path / "archive"))
    captured = []
    stub_transport(monkeypatch, "flash", captured)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def payload():
        _, review, _ = asyncio.run(route._grade_paper_core(
            batch, copy.deepcopy(paper), paper["paper_id"],
            {"name": "synthetic", "student_no": "synthetic-001", "student_id": 1}, 1,
            detail_level="detailed", grading_category="daily", grading_exam_name="", grading_mode="precise",
        ))
        assert review is not None
        # The persistent review boundary must not lose context metadata.
        batch.review_results = json.loads(json.dumps([review], ensure_ascii=False))
        return grade_review._review_group_archive_payload(batch, batch.review_results)

    with sessionmaker(bind=engine)() as db:
        db.add(User(id=1, name="synthetic-teacher", role="teacher"))
        student = Student(id=1, teacher_id=1, name="synthetic")
        db.add(student)
        db.commit()

        def persist(archive):
            result = archive["result"]
            return gp.persist_grading_result(
                db, student=student, teacher_id=1, category=archive["category"], exam_name=archive["exam_name"],
                image_path=archive["image_path"], image_hash=archive["image_hash"],
                total_questions=result["total"], correct_count=result["correct_count"],
                partial_count=result["partial_count"], wrong_count=result["wrong_count"],
                needs_review_count=result["needs_review_count"], score=result["score"], details=result["details"],
            )

        initial = payload()
        first, _, created = persist(initial)
        assert created
        old_details = copy.deepcopy(first.get_details())
        old_hash = first.grading_contract_hash
        repeated, _, created = persist(payload())
        assert not created and repeated.id == first.id
        if changed_input == "background":
            batch.answer_map["9"]["question_text"] += "教师确认的修订条件"
        else:
            monkeypatch.setattr(flash, "FLASH_TEXT_REASONING_EFFORT", "medium")
        revised_payload = payload()
        revised, _, created = persist(revised_payload)
        assert created, "An off-page reference/policy change must not replay an old archive"
        assert revised.id != first.id and revised.grading_contract_hash != old_hash
        assert first.get_details() == old_details and first.grading_contract_hash == old_hash
        replay, _, created = persist(payload())
        assert not created and replay.id == revised.id
        assert db.query(GradingResult).count() == 2
        # OCR/verdict changes are not frozen teacher policy changes.
        changed_ocr = copy.deepcopy(revised_payload["result"]["details"])
        changed_ocr[0].update(student_answer="different OCR", result="wrong", explanation="different verdict")
        assert gp.build_grading_contract_hash(changed_ocr, "daily", 2) == revised.grading_contract_hash
        assert all("text_grading_context_hash" not in json.dumps(messages) for messages, _ in captured)
    engine.dispose()


def test_historical_archive_hash_without_context_is_unchanged():
    import hashlib
    from services.grading_persistence import build_grading_contract_hash

    old_unit = {"question_no": "2", "standard_answer": "4", "grading_method": ""}
    encoded = json.dumps({"category": "daily", "scoring_version": 2, "units": [old_unit]},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    assert build_grading_contract_hash([{**old_unit, "result": "wrong"}], "daily", 2) == expected


def test_later_blank_feedback_survives_fold_review_and_export(monkeypatch, tmp_path):
    from routers import grade as route
    from routers import grade_review
    from services.error_sheet import _preferred_solution_steps
    from services.quick_grader import QuickBatch

    _, answer_map, paper = fixture_data()
    paper["questions"] = paper["questions"][:1]
    paper["questions"][0]["student_slots"][1].update(student_answer="原始转录-甲", answer_state="visible")
    batch = QuickBatch("synthetic-feedback", str(tmp_path))
    batch.answer_map = answer_map
    stub_transport(monkeypatch, "flash", [])

    async def response(messages, **kwargs):
        content = json.dumps({"results": {
            "2[1]": verdict(),
            "2[8]": {"result": "wrong", "explanation": "后一空错因", "hint": "后一空提示",
                     "model_answer": "后一空完整解答", "standard_answer": "模型回声不替代教师答案"},
        }}, ensure_ascii=False)
        kwargs["response_validator"](content)
        return {"content": content, "receipt": {}, "model": flash.FLASH_MODEL, "parameters": {}, "usage": {}}

    monkeypatch.setattr(flash, "call_flash", response)
    _, review, _ = asyncio.run(route._grade_paper_core(
        batch, paper, paper["paper_id"], {"name": "synthetic", "student_no": "001"}, 1,
        detail_level="detailed", grading_category="daily", grading_exam_name="", grading_mode="precise",
    ))
    batch.review_results = json.loads(json.dumps([review], ensure_ascii=False))
    archive = grade_review._review_group_archive_payload(batch, batch.review_results)
    detail = archive["result"]["details"][0]
    assert detail["result"] == "partial"
    assert detail["blank_results"][1]["hint"] == "后一空提示"
    assert detail["blank_results"][1]["model_answer"] == "后一空完整解答"
    assert detail["blank_results"][0]["model_answer"] == ""
    assert "2[8]" in detail["hint"] and "后一空提示" in detail["hint"]
    assert "2[8]" in _preferred_solution_steps(detail)
    assert "后一空完整解答" in _preferred_solution_steps(detail)
    assert detail["standard_answer"] == answer_map["2"]["standard_answer"]


def test_confirmation_questions_keep_the_template_grading_method(tmp_path):
    from routers import grade as route
    from services.quick_grader import QuickBatch

    batch = QuickBatch("synthetic-template", str(tmp_path))
    batch.questions = [{"question_no": "1", "question_type": "solve", "question_text": "synthetic question"}]
    batch.answer_map = {"1": {"matched": True, "grading_method": "process", "standard_answer": "2"}}
    assert route._confirmation_questions(batch)[0]["grading_method"] == "process"
