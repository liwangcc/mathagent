"""Synthetic directory integration: no live model, student files or database.

Use real session/cache/scoring functions and temporary JSON only. Failure gates
must reject conflicting IDs instead of silently keeping a later page's answer.
"""

import asyncio
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from services.grading_contract import AnswerSpec, finalize_answer_spec_for_category
from services.paper_cache import PaperCache
from services.question_directory import bind_directory_answers, parse_question_directory
from services.scoring_engine import build_grading_units, normalize_ocr_questions
from services.teaching_context import make_teaching_context, use_teaching_context
from services.template_session import TemplateSession, delete_question, insert_question


@pytest.fixture(autouse=True)
def no_live_requests(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("directory integration tests must not call any live model")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def question(number, *, kind="fill", parent="group-two", node=None, **kwargs):
    return {
        "question_no": number,
        "question_text": "SYNTHETIC_PRINTED_STEM_" + number,
        "standard_answer": "REFERENCE_SECRET_" + number,
        "question_type": kind,
        "choice_mode": "unknown",
        "directory_node_id": node or "node-" + number,
        "directory_parent_id": parent,
        "printed_path": ["二", *number.split(".")[1:]],
        "blank_count": 1, "subquestion_count": 1,
        "answer_kind": "non_graphic", "drawing_regions": [], "label_anchor": None,
        **kwargs,
    }


def pages(*groups):
    return [TemplateSession.make_page(index, rows) for index, rows in enumerate(groups)]


def by_number(page_list):
    return {row["question_no"]: row for page in page_list for row in page["questions"]}


LEGACY_SPEC = {
    "question_no": "17", "question_text": "legacy ___", "question_type": "fill",
    "choice_mode": "unknown", "standard_answer": "legacy-reference", "answer_variants": [],
    "solution_steps": [], "max_score": None, "blank_count": 1, "answer_slots": [],
    "fill_scoring_mode": "equal", "blank_scores": [], "subquestion_count": 1,
    "subquestion_scores": [], "grading_method": "equivalent", "chapter": "", "topic": "",
    "correction_excluded": False, "matched": True, "question_bank_id": None,
    "difficulty": 3, "source": "teacher_confirmed",
}


def test_legacy_answer_spec_dump_does_not_inject_directory_defaults():
    spec = AnswerSpec.model_validate({"question_no": "17", "question_text": "legacy ___",
                                     "question_type": "fill", "standard_answer": "legacy-reference"})
    assert spec.model_dump() == LEGACY_SPEC
    assert AnswerSpec.model_validate(LEGACY_SPEC).model_dump() == LEGACY_SPEC
    for key in ("directory_node_id", "directory_parent_id", "printed_path", "answer_kind", "drawing_regions", "label_anchor"):
        assert key not in spec.to_answer_map_entry()


@pytest.mark.parametrize("options,expected", [
    ({"include": {"question_no"}}, {"question_no": "2.3"}),
    ({"include": {"question_no", "directory_node_id"}}, {"question_no": "2.3", "directory_node_id": "node-2.3"}),
    ({"include": {"question_no", "printed_path"}, "exclude": {"printed_path"}}, {"question_no": "2.3"}),
    ({"include": {"printed_path": {0}}}, {"printed_path": ["二"]}),
    ({"include": {"drawing_regions"}, "exclude": {"drawing_regions": {1}}}, {"drawing_regions": [[700, 350, 900, 550]]}),
])
def test_answer_spec_model_dump_respects_include_exclude_without_metadata_reinjection(options, expected):
    spec = AnswerSpec.model_validate(question("2.3", kind="solve", answer_kind="mixed",
                                             drawing_regions=[[700, 350, 900, 550], [100, 700, 400, 900]],
                                             label_anchor=[690, 400]))
    assert spec.model_dump(**options) == expected


def test_answer_spec_exclude_none_does_not_reintroduce_root_parent_or_null_anchor():
    spec = AnswerSpec.model_validate(question("3", parent=None))
    dumped = spec.model_dump(exclude_none=True)
    assert dumped["directory_node_id"] == "node-3"
    assert "directory_parent_id" not in dumped and "label_anchor" not in dumped


def test_legacy_confirmation_content_hash_and_roundtrip_remain_stable(tmp_path):
    session = TemplateSession("legacy-hash", str(tmp_path), teacher_id=7)
    session.category = "daily"
    session.add_page(0, [LEGACY_SPEC])
    assert session.pages[0]["questions"] == [LEGACY_SPEC]
    old_payload = {"category": "daily", "exam_name": "", "template_mode": "marked",
                   "column_mode": "double", "pages": session.pages}
    legacy_hash = hashlib.sha256(json.dumps(old_payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    assert session.content_hash() == legacy_hash
    session.confirm_current_contract(7)
    session.status = "confirmed"
    session.save_meta()
    reopened = TemplateSession.load_meta("legacy-hash", str(tmp_path))
    reopened.pages = [TemplateSession.make_page(0, reopened.pages[0]["questions"])]
    assert reopened.confirmed_contract_hash == reopened.content_hash() == legacy_hash
    assert reopened.recognition_contract_error() == ""


@pytest.mark.parametrize("stage", ["primary", "junior", "senior"])
def test_two_pages_continuous_25_qs_survive_session_cache_and_frozen_stage(tmp_path, stage):
    context = make_teaching_context(stage)
    session = TemplateSession("multi-" + stage, str(tmp_path), teacher_id=7, teaching_context=context)
    session.category = "daily"
    session.add_page(0, [question(str(i), parent=None, printed_path=[str(i)]) for i in range(1, 13)])
    session.add_page(1, [question(str(i), parent=None, printed_path=[str(i)]) for i in range(13, 26)])
    session.question_directories = {"page_1": {"version": "audit-only", "original_node_count": 12},
                                    "page_2": {"version": "audit-only", "original_node_count": 13}}
    session.confirm_current_contract(7)
    session.status = "confirmed"
    session.save_meta()
    reopened = TemplateSession.load_meta("multi-" + stage, str(tmp_path))
    assert reopened.teaching_context == context
    assert reopened.question_directories == session.question_directories
    assert reopened.recognition_contract_error() == ""
    answer_map = reopened.build_answer_map()
    assert list(answer_map) == [str(i) for i in range(1, 26)]
    assert all(row["education_stage"] == stage for row in answer_map.values())
    assert all(row["standard_answer"] == "REFERENCE_SECRET_" + number for number, row in answer_map.items())
    cache = PaperCache.from_confirmed_template_pages(list(reversed(reopened.pages)))
    assert cache._expected_question_numbers("page_1") == [str(i) for i in range(1, 13)]
    assert cache._expected_question_numbers("page_2") == [str(i) for i in range(13, 26)]
    assert cache._resolve_joint_page_type({"page_type_id": "page_2", "observed_question_numbers": ["13"]}) == ("page_2", ["13"])
    with use_teaching_context(context):
        prompt = cache._build_page_and_answers_prompt()
    assert "REFERENCE_SECRET_" not in prompt
    assert "SYNTHETIC_PRINTED_STEM_" not in prompt


def test_directory_metadata_and_drawing_are_hash_bound_but_original_tree_is_audit_only(tmp_path):
    session = TemplateSession("new-hash", str(tmp_path), teacher_id=7)
    row = question("2.3", kind="solve", printed_path=["二", None], answer_kind="mixed",
                   drawing_regions=[[700, 350, 900, 550]], label_anchor=[690, 400])
    session.add_page(0, [row])
    session.page_artifacts["page_1"] = {"version": 1, "sha256": "a" * 64, "width": 1000, "height": 1400,
                                         "question_schema": [deepcopy(session.pages[0]["questions"][0])], "question_numbers": ["2.3"]}
    session.question_directories["page_1"] = {"nodes": [{"context_summary": "original tree"}]}
    content_hash, artifact_hash = session.content_hash(), session.artifact_hash()
    session.question_directories["page_1"]["nodes"][0]["context_summary"] = "audit note only"
    assert session.content_hash() == content_hash
    assert session.artifact_hash() == artifact_hash
    session.confirm_current_contract(7)
    session.save_meta()
    loaded = TemplateSession.load_meta("new-hash", str(tmp_path))
    restored = loaded.pages[0]["questions"][0]
    for key in ("directory_node_id", "directory_parent_id", "printed_path", "answer_kind", "drawing_regions", "label_anchor"):
        assert restored[key] == row[key]
    assert loaded.content_hash() == content_hash
    loaded.pages[0]["questions"][0]["label_anchor"] = [680, 400]
    assert loaded.content_hash() != content_hash
    loaded.page_artifacts["page_1"]["question_schema"][0]["label_anchor"] = [680, 400]
    assert loaded.artifact_hash() != artifact_hash
    assert loaded.recognition_contract_error()


def test_insert_leaf_shifts_only_same_parent_across_pages_without_rebinding_answers():
    original = pages([question("2.1.1"), question("2.1.2"), question("2.2", parent="outer-two")],
                     [question("2.1.3"), question("3.1", parent="group-three")])
    snapshot = deepcopy(original)
    changed, inserted, renames = insert_question(original, page_index=0, before_question_number="2.1.2")
    assert original == snapshot
    assert inserted == "2.1.2"
    assert renames == {"2.1.2": "2.1.3", "2.1.3": "2.1.4"}
    indexed = by_number(changed)
    assert len(indexed) == 6
    assert indexed[inserted]["standard_answer"] == ""
    assert indexed[inserted]["directory_parent_id"] == "group-two"
    for before, after in renames.items():
        assert indexed[after]["standard_answer"] == "REFERENCE_SECRET_" + before
        assert indexed[after]["directory_node_id"] == "node-" + before
        assert indexed[after]["printed_path"] == by_number(original)[before]["printed_path"]
    for untouched in ("2.1.1", "2.2", "3.1"):
        assert indexed[untouched] == by_number(original)[untouched]


def test_delete_leaf_leaves_remaining_numbers_answers_and_groups_unchanged():
    original = pages([question("2.1.1"), question("2.1.2"), question("2.1.3")], [question("3.1", parent="three")])
    snapshot = deepcopy(original)
    changed = delete_question(original, page_index=0, question_number="2.1.2")
    assert original == snapshot
    assert list(by_number(changed)) == ["2.1.1", "2.1.3", "3.1"]
    assert all(row == by_number(original)[number] for number, row in by_number(changed).items())


def test_old_continuous_insertion_across_pages_keeps_each_answer_attached():
    original = pages([question("1"), question("2")], [question("3"), question("4")])
    changed, inserted, renames = insert_question(original, page_index=0, before_question_number="2")
    assert inserted == "2" and renames == {"2": "3", "3": "4", "4": "5"}
    assert [by_number(changed)[str(i)]["standard_answer"] for i in range(1, 6)] == [
        "REFERENCE_SECRET_1", "", "REFERENCE_SECRET_2", "REFERENCE_SECRET_3", "REFERENCE_SECRET_4"]


def test_continuous_directory_insertion_assigns_new_leaf_identity_not_legacy_row():
    original = pages([question("1", parent=None, printed_path=["1"]),
                      question("2", parent=None, printed_path=["2"])],
                     [question("3", parent=None, printed_path=["3"])])
    changed, new_number, renames = insert_question(original, page_index=0, before_question_number="2")
    inserted = by_number(changed)[new_number]
    assert new_number == "2" and renames == {"2": "3", "3": "4"}
    assert inserted["directory_node_id"]
    assert inserted["directory_node_id"] not in {row["directory_node_id"] for row in by_number(original).values()}
    assert inserted["directory_parent_id"] is None
    # Q2 is the new internal identity, not proof of an observed printed label.
    assert inserted["printed_path"] == [None]
    assert inserted["blank_count"] == inserted["subquestion_count"] == 1
    assert inserted["standard_answer"] == ""
    with pytest.raises(ValueError, match="末级题"):
        AnswerSpec.model_validate({**inserted, "subquestion_count": 2})
    assert by_number(changed)["3"]["directory_node_id"] == "node-2"
    assert by_number(changed)["4"]["standard_answer"] == "REFERENCE_SECRET_3"


def test_genuinely_legacy_continuous_insert_does_not_inject_new_directory_defaults():
    original = pages([{"question_no": "1", "question_text": "old-1", "standard_answer": "old-A"},
                      {"question_no": "2", "question_text": "old-2", "standard_answer": "old-B"}])
    changed, new_number, renames = insert_question(original, page_index=0, before_question_number="2")
    assert new_number == "2" and renames == {"2": "3"}
    assert by_number(changed)["3"]["standard_answer"] == "old-B"
    for row in by_number(changed).values():
        assert "directory_node_id" not in row
        assert "directory_parent_id" not in row
        assert "printed_path" not in row


def test_duplicate_q_across_pages_must_not_silently_overwrite_answer_map(tmp_path):
    session = TemplateSession("duplicate-leaf", str(tmp_path))
    # Simulate an already persisted old/colliding snapshot, bypassing add_page.
    session.pages = pages([question("2.1.1", standard_answer="FIRST_PAGE")],
                          [question("2.1.1", standard_answer="SECOND_PAGE")])
    with pytest.raises(ValueError, match="题号|duplicate"):
        session.build_answer_map()


def test_duplicate_q_across_pages_cannot_enter_frozen_ocr_cache():
    with pytest.raises(ValueError, match="题号|duplicate"):
        PaperCache.from_confirmed_template_pages(pages([question("2.1.1")], [question("2.1.1")]))


def test_independent_page_directories_expose_restart_collision_for_global_gate(tmp_path):
    def directory(label):
        return parse_question_directory({"coordinate_space": "normalized_1000_v1", "needs_review": [], "nodes": [
            {"printed_label": label, "kind": "section", "context_summary": "分类", "number_bbox": None,
             "question_type": None, "regions": [], "exemptions": [], "children": [
                 {"printed_label": "1", "kind": "question", "context_summary": "独立填写题", "number_bbox": [1, 1, 5, 5],
                  "question_type": "fill", "regions": [[20, 20, 30, 30]], "exemptions": [], "children": []}]}]})

    session = TemplateSession("restart-page", str(tmp_path))
    for index, label in enumerate(("一", "二")):
        parsed = directory(label)
        bound = bind_directory_answers(parsed, {"answers": [{"q_id": "Q1", "question_text": "题干" + label,
                  "standard_answer": "答案" + label, "solution_steps": "", "answer_status": "ready"}], "directory_issues": []})
        session.pages.append(TemplateSession.make_page(index, bound))
    # Each local extraction is valid; only a document-level gate can reject this.
    with pytest.raises(ValueError, match="题号|duplicate"):
        session.build_answer_map()


@pytest.mark.parametrize("category", ["daily", "exam"])
def test_confirmed_leaf_scoring_never_reexpands_shared_numbered_context(category):
    raw = [question("2.1.1", question_text="共同题干(1)___ (2)___ (3)___", standard_answer="5,3"),
           question("2.1.2", question_text="共同题干(1)___ (2)___ (3)___", standard_answer="7,6"),
           question("2.2", kind="solve", question_text="共同材料(1)说明(2)计算(3)画图", standard_answer="只批该叶题"),
           question("3", kind="solve", answer_kind="mixed", question_text="同一无编号综合题画图、计算、说明")]
    specs = [finalize_answer_spec_for_category(row, category) for row in raw]
    answer_map = {spec.question_no: spec.to_answer_map_entry() for spec in specs}
    ocr = [{"question_number": row["question_no"], "student_answer": "student-" + row["question_no"]} for row in raw]
    normalized = normalize_ocr_questions(ocr, answer_map)
    assert [row["question_no"] for row in normalized] == [row["question_no"] for row in raw]
    units = build_grading_units(ocr, answer_map, category)
    assert [unit["key"] for unit in units] == [row["question_no"] for row in raw]
    assert all(unit["parent_question_no"] == unit["key"] for unit in units)
    assert all(unit["entry"]["directory_parent_id"] == "group-two" for unit in units)


def test_exam_leaf_without_explicit_scores_cannot_infer_extra_parts_from_shared_stem():
    spec = AnswerSpec.model_validate(question("2.3", kind="solve", question_text="共同题干(1)填写(2)比较(3)作图"))
    assert spec.subquestion_count == 1 and spec.subquestion_scores == []
    units = build_grading_units([{"question_no": "2.3", "student_answer": "学生作图"}],
                                {"2.3": spec.to_answer_map_entry()}, "exam")
    assert [unit["key"] for unit in units] == ["2.3"]


@pytest.mark.parametrize("field,value", [("subquestion_count", 3), ("subquestion_scores", [1, 2])])
def test_answer_contract_rejects_attempt_to_reexpand_directory_leaf(field, value):
    with pytest.raises(ValueError, match="末级题"):
        AnswerSpec.model_validate(question("2.1.1", **{field: value}))


def test_directory_group_keeps_independent_regions_without_another_question_level():
    spec = AnswerSpec.model_validate(question("1", blank_count=3, answer_slots=["2", "3", "2"],
                                            blank_scores=[1, 2, 1]))
    assert spec.blank_count == 3 and spec.subquestion_count == 1
    assert spec.answer_slots == ["2", "3", "2"]
    assert spec.blank_scores == [1, 2, 1]


@pytest.mark.parametrize("strategy", ["full_column_difference", "full_height_columns", "continuous_question_crop_plan"])
def test_difference_ocr_prompt_preserves_leaf_ids_and_printed_paths_not_answers(strategy):
    rows = [question("2.1.1", printed_path=["二", "1", "①"]),
            question("2.1.2", printed_path=["二", "1", "②"]),
            question("2.3", kind="solve", answer_kind="drawing", printed_path=["二", "3"])]
    cache = PaperCache.from_confirmed_template_pages(pages(rows))
    prompt = cache._build_evidence_only_prompt("page_1", evidence_strategy=strategy)
    assert '题号清单=["2.1.1","2.1.2","2.3"]' in prompt
    assert '作答类型={"2.1.1":"fill","2.1.2":"fill","2.3":"drawing"}' in prompt
    assert "printed_path" not in prompt
    assert "REFERENCE_SECRET_" not in prompt and "SYNTHETIC_PRINTED_STEM_" not in prompt
    assert "standard_answer" not in prompt
    schema = cache._question_schema_for_page("page_1")
    assert [row["question_number"] for row in schema] == ["2.1.1", "2.1.2", "2.3"]
    assert schema[-1]["answer_kind"] == "drawing"
    assert all(row.get("blank_count", 1) == row.get("subquestion_count", 1) == 1 for row in schema)


def test_joint_ocr_prompt_cannot_leak_reference_answer_slots_as_shape_metadata():
    cache = PaperCache.from_confirmed_template_pages(pages([
        question("2.1.1", answer_slots=["ANSWER_SLOT_REFERENCE_SECRET"], answer_variants=["ANSWER_VARIANT_SECRET"])]))
    prompt = cache._build_page_and_answers_prompt()
    assert "REFERENCE_SECRET_" not in prompt
    assert "ANSWER_VARIANT_SECRET" not in prompt
    assert "ANSWER_SLOT_REFERENCE_SECRET" not in prompt


def test_actual_ocr_merge_keeps_exact_leaf_ids_and_never_binds_by_printed_label(monkeypatch):
    import services.ocr_service as ocr_service

    rows = [question("2.1.1"), question("2.1.2"), question("2.1.3")]
    cache = PaperCache.from_confirmed_template_pages(pages(rows))
    calls = []

    async def fake_vision(_path, prompt, **_kwargs):
        calls.append(prompt)
        return json.dumps({"questions": [{"question_number": row["question_no"],
                          "student_answer": "actual-" + row["question_no"], "answer_state": "visible"} for row in rows]})

    monkeypatch.setattr(ocr_service, "call_vision", fake_vision)
    result = asyncio.run(cache._recognize_answers_only("synthetic-not-read.png", "page_1", evidence_mode=True,
                                                       evidence_strategy="full_column_difference"))
    assert "error" not in result and len(calls) == 1
    assert [row["question_number"] for row in result["questions"]] == ["2.1.1", "2.1.2", "2.1.3"]
    assert [row["student_answer"] for row in result["questions"]] == ["actual-2.1.1", "actual-2.1.2", "actual-2.1.3"]
    assert result["_requires_separate_grading"] and result["_transcription_only"]
    assert "REFERENCE_SECRET_" not in calls[0]


def test_hierarchical_leaf_pages_are_distinct_for_cache_pages_and_joint_routing():
    cache = PaperCache.from_confirmed_template_pages(pages([question("2.1.1"), question("2.1.2")],
                                                            [question("2.1.3"), question("2.2")]))
    assert {
        label: [q["question_number"] for q in baseline["ocr_result"]["questions"]]
        for label, baseline in cache._baselines.items()
    } == {"page_1": ["2.1.1", "2.1.2"], "page_2": ["2.1.3", "2.2"]}
    assert cache._resolve_joint_page_type({"page_type_id": "page_1", "observed_question_numbers": ["2.1.2"]}) == ("page_1", ["2.1.2"])
    assert cache._resolve_joint_page_type({"page_type_id": "page_2", "observed_question_numbers": ["2.1.3"]}) == ("page_2", ["2.1.3"])
    with pytest.raises(ValueError):
        cache._resolve_joint_page_type({"page_type_id": "page_2", "observed_question_numbers": ["2.1"]})


@pytest.mark.parametrize("numbers", [
    ["2.1.1", "2.1.2"],
    ["2.1.1", "2.1.2", "2.1.2"],
    ["2.1.3", "2.1.2", "2.1.1"],
    ["Q2.1.1", "Q2.1.2", "Q2.1.3"],
    ["2.1"],
])
def test_ocr_omission_reorder_duplicate_or_parent_id_cannot_silently_bind(monkeypatch, numbers):
    import services.ocr_service as ocr_service

    cache = PaperCache.from_confirmed_template_pages(pages([question("2.1.1"), question("2.1.2"), question("2.1.3")]))
    snapshot = deepcopy(cache._baselines)

    async def fake_vision(*_args, **_kwargs):
        return json.dumps({"questions": [{"question_number": number, "student_answer": "visible actual",
                                          "answer_state": "visible"} for number in numbers]})

    monkeypatch.setattr(ocr_service, "call_vision", fake_vision)
    result = asyncio.run(cache._recognize_answers_only("synthetic-not-read.png", "page_1", evidence_mode=True,
                                                       evidence_strategy="full_column_difference"))
    assert result.get("error")
    assert not result.get("questions")
    assert cache._baselines == snapshot
