"""Directory leaves score by answer regions; stale subquestion layers must merge.

Regression for production template d84978056d60: its persisted leaf row carried
subquestion_count=1 with a stale multi-entry layer ``subquestion_scores=[5, 0]``
and crashed ``AnswerSpec`` validation at template confirm
("目录末级题按作答区域计分，不能另建小问层级"), surfacing as a misleading
"模型返回结果不符合格式要求".

Contract boundaries:
- ``AnswerSpec`` stays strict: a submission carrying a score layer on a leaf is
  rejected (see also test_answer_contract_rejects_attempt_to_reexpand_directory_leaf).
- Producers and the template load boundary own the sanctioned merge
  (已有小问分值合成根节点总分): rows never reach the gate with a stale layer.
"""
import json
import os

import pytest

from services.grading_contract import (
    AnswerSpec,
    finalize_answer_spec_for_category,
    merge_directory_leaf_score_layer,
)
from services.question_directory import (
    COORDINATE_SPACE,
    DIRECTORY_VERSION,
    bind_directory_answers,
)
from services.template_session import TemplateSession


def _legacy_leaf_row() -> dict:
    # The exact stored shape recovered from production template d84978056d60.
    return {
        "question_no": "1",
        "question_type": "fill",
        "answer_kind": "non_graphic",
        "blank_count": 2,
        "answer_slots": ["2/5 + 2/5 = 4/5", "2/5 × 2 = 4/5"],
        "slot_ids": ["1[1]", "1[2]"],
        "region_kinds": ["answer", "answer"],
        "fill_scoring_mode": "equal",
        "blank_scores": [2.5, 2.5],
        "subquestion_count": 1,
        "subquestion_scores": [5, 0],
        "max_score": None,
        "directory_node_id": "n001",
        "directory_parent_id": None,
        "printed_path": ["【基础练习】", "1."],
        "question_text": "t",
        "standard_answer": "s",
    }


def test_score_layer_helper_folds_into_root_total():
    healed = merge_directory_leaf_score_layer(_legacy_leaf_row())
    assert healed["subquestion_count"] == 1
    assert healed["subquestion_scores"] == []
    assert healed["max_score"] == 5.0


def test_score_layer_helper_folds_slot_edit_echo_layers():
    # updateSlotStructure emits subquestion_count = slot count with mirrored
    # scores on solve-leaf slot edits; on a leaf the whole layer folds.
    row = _legacy_leaf_row()
    row["subquestion_count"] = 2
    row["subquestion_scores"] = [5, 5]
    healed = merge_directory_leaf_score_layer(row)
    assert healed["subquestion_count"] == 1
    assert healed["subquestion_scores"] == []
    # The teacher's blank_scores ([2.5, 2.5]) stay authoritative: the junk
    # mirror sum [5, 5] must never turn into a 10-point total.
    assert healed["max_score"] == 5.0
    AnswerSpec.model_validate(healed)


def _exam_entry(row):
    spec = finalize_answer_spec_for_category(AnswerSpec.model_validate(dict(row)), "exam")
    return spec.to_answer_map_entry()


def _fill_leaf_row():
    return {
        "question_number": "1", "question_type": "fill",
        "directory_node_id": "n001", "directory_parent_id": None,
        "max_score": None, "fill_scoring_mode": "custom",
        "subquestion_count": 1, "subquestion_scores": [],
        "blank_count": 2, "blank_scores": [2.5, 2.5],
        "slot_ids": ["1[1]", "1[2]"], "region_kinds": ["answer", "answer"],
        "answer_slots": ["2", "3"],
    }


def _solve_leaf_row():
    return {
        "question_number": "2", "question_type": "solve",
        "directory_node_id": "n002", "directory_parent_id": None,
        "max_score": None,
        "subquestion_count": 1, "subquestion_scores": [3.0, 2.0],
        "blank_count": 2, "blank_scores": [],
        "slot_ids": ["2[1]", "2[2]"], "region_kinds": ["answer", "answer"],
        "answer_slots": ["x=1", "y=2"],
    }


def test_fill_leaf_echo_is_grading_equivalent_to_clean_row():
    # An editor echo on a fill leaf must be grading-equivalent to the clean
    # edit: the junk subquestion mirror never changes what scoring sees.
    clean = _fill_leaf_row()
    echo = dict(clean, subquestion_count=2, subquestion_scores=[5, 5])
    assert _exam_entry(merge_directory_leaf_score_layer(echo)) == _exam_entry(clean)


def test_solve_leaf_region_scores_pass_through_verbatim():
    # A solve leaf scores by its answer regions: the region-aligned score list
    # IS that channel (scoring_engine aligns it to the answer boxes) and must
    # reach grading byte-for-byte; only the hierarchy marker normalizes.
    clean = _solve_leaf_row()
    echo = dict(clean, subquestion_count=2)
    healed = merge_directory_leaf_score_layer(echo)
    assert healed["subquestion_count"] == 1
    assert healed["subquestion_scores"] == [3.0, 2.0]
    assert _exam_entry(healed) == _exam_entry(clean)


def test_solve_leaf_unaligned_scores_stay_rejected():
    # No alignment evidence → still a subquestion hierarchy attempt.
    import pytest
    row = dict(_solve_leaf_row(), blank_count=1, slot_ids=["2[1]"], region_kinds=["answer"],
               answer_slots=["x=1"], subquestion_scores=[1, 2])
    with pytest.raises(ValueError, match="末级题"):
        AnswerSpec.model_validate(row)


def test_score_layer_helper_keeps_real_layers_on_non_directory_rows():
    row = _legacy_leaf_row()
    row.pop("directory_node_id")
    row["subquestion_count"] = 2
    row["subquestion_scores"] = [5, 5]
    untouched = merge_directory_leaf_score_layer(row)
    assert untouched["subquestion_count"] == 2
    assert untouched["subquestion_scores"] == [5, 5]


def test_strict_contract_gate_still_rejects_submitted_score_layers():
    # The merge must never weaken the submission gate: rows are healed before
    # reaching AnswerSpec, and any submitted layer stays a hard rejection.
    with pytest.raises(ValueError, match="末级题"):
        AnswerSpec.model_validate(_legacy_leaf_row())


def test_template_load_heals_persisted_legacy_rows_before_the_gate(tmp_path):
    session = TemplateSession("legacy", str(tmp_path))
    os.makedirs(os.path.dirname(session.meta_path), exist_ok=True)
    with open(session.meta_path, "w", encoding="utf-8") as handle:
        json.dump({"pages": [{"page_index": 0, "questions": [_legacy_leaf_row()]}]}, handle)

    loaded = TemplateSession.load_meta("legacy", str(tmp_path))
    healed = loaded.pages[0]["questions"][0]
    assert healed["subquestion_scores"] == []
    assert healed["max_score"] == 5.0
    spec = AnswerSpec.model_validate(healed)
    assert spec.subquestion_count == 1
    assert spec.subquestion_scores == []


def test_directory_answer_binding_never_leaks_a_score_layer():
    leaf = {
        "question_number": "1",
        "q_id": "Q1",
        "node_id": "n001",
        "parent_id": None,
        "subquestion_scores": [5, 0],
        "subquestion_count": 1,
    }
    directory = {
        "version": DIRECTORY_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "leaves": [leaf],
        "needs_review": [],
    }
    rows = bind_directory_answers(
        directory,
        {
            "answers": [
                {
                    "q_id": "Q1",
                    "question_text": "t",
                    "standard_answer": "s",
                    "answer_status": "ready",
                }
            ],
            "directory_issues": [],
        },
    )
    assert rows[0]["subquestion_count"] == 1
    assert rows[0]["subquestion_scores"] == []
    assert merge_directory_leaf_score_layer(dict(rows[0]))["subquestion_scores"] == []
