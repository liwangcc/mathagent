"""Synthetic metadata only: stage survives saves without touching real uploads."""
import hashlib
import json

from services.answer_sheet_session import AnswerSheetSession
from services.quick_grader import QuickBatch
from services.teaching_context import make_teaching_context, use_teaching_context
from services.template_session import TemplateSession


def question():
    return {
        "question_no": "1", "question_text": "计算 1+1。",
        "question_type": "fill", "standard_answer": "2",
    }


def test_template_snapshot_and_confirmation_survive_profile_change(tmp_path):
    context = make_teaching_context("junior")
    session = TemplateSession("stage-test", str(tmp_path), 1, teaching_context=context)
    session.add_page(0, [question()])
    session.confirm_current_contract(1)
    frozen_hash = session.confirmed_contract_hash
    session.save_meta()
    with use_teaching_context(make_teaching_context("senior")):
        loaded = TemplateSession.load_meta("stage-test", str(tmp_path))
        assert loaded.teaching_context == context
        assert loaded.content_hash() == frozen_hash
    loaded.teaching_context = make_teaching_context("senior")
    assert loaded.content_hash() != frozen_hash


def test_legacy_template_hash_does_not_change(tmp_path):
    session = TemplateSession("legacy-test", str(tmp_path), 1)
    session.add_page(0, [question()])
    original_payload = {
        "category": session.category, "exam_name": session.exam_name,
        "template_mode": session.template_mode, "column_mode": session.column_mode,
        "pages": session.pages,
    }
    old_hash = hashlib.sha256(json.dumps(
        original_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert session.content_hash() == old_hash
    with use_teaching_context(make_teaching_context("junior")):
        # A profile/request context never labels an existing legacy task.
        session.save_meta()
        loaded = TemplateSession.load_meta("legacy-test", str(tmp_path))
        assert loaded.teaching_context == {}
        assert loaded.content_hash() == old_hash


def test_visual_label_policy_is_frozen_without_invalidating_legacy(tmp_path):
    session = TemplateSession("policy-test", str(tmp_path), 1)
    artifact = {"version": 1, "sha256": "a" * 64, "width": 100, "height": 200,
                "question_schema": [], "question_numbers": []}
    session.page_artifacts["page_1"] = artifact
    old_payload = [{"page_label": "page_1", **artifact}]
    old_hash = hashlib.sha256(json.dumps(
        old_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert session.artifact_hash() == old_hash
    session.confirm_current_contract(1)
    session.save_meta()
    loaded = TemplateSession.load_meta("policy-test", str(tmp_path))
    assert loaded.recognition_contract_error() == ""
    loaded.page_artifacts["page_1"]["question_label_policy"] = "printed-solve-v1"
    assert loaded.artifact_hash() != old_hash
    assert loaded.recognition_contract_error()
    loaded.confirm_current_contract(1)
    assert loaded.recognition_contract_error() == ""
    del loaded.page_artifacts["page_1"]["question_label_policy"]
    assert loaded.recognition_contract_error()


def test_quick_batch_keeps_template_stage_on_restore(tmp_path):
    context = make_teaching_context("junior")
    batch = QuickBatch("stage-batch", str(tmp_path), teaching_context=context)
    batch.identity_mode = "anonymous"
    batch.save_meta()
    with use_teaching_context(make_teaching_context("senior")):
        loaded = QuickBatch.load_meta("stage-batch", str(tmp_path))
    assert loaded.teaching_context == context
    assert loaded.identity_mode == "anonymous"


def test_answer_sheet_semantic_hash_binds_stage_not_shared_geometry(tmp_path):
    signatures = set()
    for stage in ("junior", "senior"):
        context = make_teaching_context(stage)
        session = AnswerSheetSession("a" * 16, str(tmp_path / stage), teaching_context=context)
        session.status = "source_recognized"
        session.confirm_answers([question()])
        signatures.add(session.answer_spec_hash)
        session.save()
        loaded = AnswerSheetSession.load(session.session_id, session.upload_dir)
        assert loaded.teaching_context == context
        assert loaded.answer_spec_hash == session.answer_spec_hash
    assert len(signatures) == 2
