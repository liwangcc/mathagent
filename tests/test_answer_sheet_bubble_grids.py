"""Slice 3: model-choice bubble-card annotation contract.

Bubble grids are answer-sheet-template data.  A model may only propose where
a grid is; the teacher's explicit confirm/adjust (stamped with who and when)
makes a row real; confirmed rows are frozen into the template, never copied
into a grading session, and resolved to pixel boxes from the template at
grading time.  Anything unconfirmed fails closed.
"""
import hashlib
import json
import random
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

import services.answer_sheet_template_registry as registry_module
from services import answer_sheet_session as session_module
from services.answer_sheet_identity import crop_normalized_rgb
from services.answer_sheet_layout_contract import (
    bubble_grid_coverage,
    choice_row_keys,
    freeze_grid_rows,
    normalized_bbox_to_pixels,
)
from services.answer_sheet_session import BATCH_CONTRACT_VERSION, AnswerSheetSession
from services.answer_sheet_template_registry import (
    AnswerSheetTemplateIncompatible,
    AnswerSheetTemplateRegistry,
)
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_evidence import build_template_artifact
from tests.answer_sheet_redesign_helpers import (
    install_registries,
    teacher_confirmed_bubble_grid,
)
from tests.test_answer_sheet_session import client_env  # noqa: F401


PAGE_SIZE = (800, 1200)
Q1_REGION = [100, 200, 500, 260]
Q2_REGION = [100, 300, 500, 420]
Q2_ROW_1 = [100, 300, 500, 350]
Q2_ROW_2 = [100, 360, 500, 410]
FILL_REGION = [100, 500, 900, 600]
IDENTITY = [{
    "region_id": "identity_1",
    "page_id": "page_1",
    "bbox": [80, 60, 420, 150],
    "identity_type": "name",
    "teacher_confirmed": True,
}]


def _specs(*, q2_small_questions: int = 2) -> list[dict]:
    """Q1 whole choice, Q2 choice with declared small questions, Q3 fill."""
    return [
        {
            "question_no": "1", "question_type": "choice", "choice_mode": "single",
            "standard_answer": "C", "subquestion_count": 1,
        },
        {
            "question_no": "2", "question_type": "choice", "choice_mode": "single",
            "standard_answer": "A", "subquestion_count": q2_small_questions,
        },
        {"question_no": "3", "question_type": "fill", "standard_answer": "1/2"},
    ]


def _regions(q1_bbox: list[int] = Q1_REGION) -> list[dict]:
    choice = {
        "region_type": "choice_mark",
        "teacher_confirmed": True,
        "choice_options": ["A", "B", "C", "D"],
    }
    return [
        {"region_id": "answer_1", "page_id": "page_1", "question_no": "1",
         "bbox": list(q1_bbox), **choice},
        {"region_id": "answer_2", "page_id": "page_1", "question_no": "2",
         "bbox": list(Q2_REGION), **choice},
        {"region_id": "answer_3", "page_id": "page_1", "question_no": "3",
         "bbox": list(FILL_REGION), "region_type": "fill", "teacher_confirmed": True},
    ]


def _all_rows() -> list[dict]:
    return [
        teacher_confirmed_bubble_grid("1", Q1_REGION),
        teacher_confirmed_bubble_grid("2", Q2_ROW_1, subquestion_no="1"),
        teacher_confirmed_bubble_grid("2", Q2_ROW_2, subquestion_no="2"),
    ]


def _session(upload_dir, session_id: str, *, teacher_id: int = 1, batch: bool = True,
             specs: list[dict] | None = None) -> AnswerSheetSession:
    session = AnswerSheetSession(session_id, str(upload_dir), teacher_id=teacher_id, class_id=10)
    if batch:
        session.batch_contract_version = BATCH_CONTRACT_VERSION
    session.answer_spec_snapshot = specs if specs is not None else _specs()
    session.answer_spec_hash = hashlib.sha256(
        json.dumps(session.answer_spec_snapshot, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    session.status = "answers_confirmed"
    session.save()
    return session


def _draft(upload_dir, session_id: str = "a" * 16, **kwargs) -> AnswerSheetSession:
    """A layout draft with two choice_mark regions and one fill region."""
    session = _session(upload_dir, session_id, **kwargs)
    page_path = Path(session.session_dir) / "layout" / "page.png"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", PAGE_SIZE, "white").save(page_path)
    digest = hashlib.sha256(page_path.read_bytes()).hexdigest()
    session.answer_sheet_pages = [{
        "page_id": "page_1", "index": 0, "path": str(page_path),
        "working_path": str(page_path), "sha256": digest, "working_sha256": digest,
    }]
    session.save_layout(_regions(), IDENTITY)
    return session


def _freeze(session: AnswerSheetSession) -> None:
    page_path = session.answer_sheet_pages[0]["working_path"]
    artifact = build_template_artifact(
        page_path,
        session.session_dir,
        0,
        [{
            "question_number": region["region_id"],
            "question_type": "choice" if region["region_type"].startswith("choice_") else region["region_type"],
            "answer_bbox": region["bbox"],
        } for region in session.answer_regions],
    )
    artifact["question_crop_plan"] = build_anchor_crop_plan(
        {"column_layout": {
            "column_count": 1,
            "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        }},
        artifact_sha256=artifact["sha256"],
        expected_question_numbers=[region["question_no"] for region in session.answer_regions],
    )
    session.layout_artifacts = {"page_1": artifact}
    session.freeze_layout()


def _published(tmp_path, registry: AnswerSheetTemplateRegistry, session_id: str = "a" * 16,
               *, teacher_id: int = 1) -> tuple[AnswerSheetSession, dict]:
    source = _draft(tmp_path / "authoring", session_id, teacher_id=teacher_id)
    source.confirm_bubble_grids(_all_rows(), teacher_id=teacher_id)
    _freeze(source)
    return source, registry.publish(source, creator_teacher_id=teacher_id)


def _bound_batch(tmp_path, registry, code: str, session_id: str = "b" * 16,
                 *, specs: list[dict] | None = None) -> AnswerSheetSession:
    batch = _session(tmp_path / "uploads", session_id, specs=specs)
    registry.apply_to_session(batch, code)
    return batch


def _manifest_path(registry: AnswerSheetTemplateRegistry, code: str) -> Path:
    return registry.templates_dir / code / "manifest.json"


def _set_clock(monkeypatch, moment: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.replace(tzinfo=tz)

    monkeypatch.setattr(session_module, "datetime", Clock)


# --- placement proposal / confirmation state machine -----------------------

def test_model_proposal_is_unconfirmed_and_blocks_freeze(tmp_path):
    session = _draft(tmp_path)
    skipped = session.propose_bubble_grids(
        [{"question_no": "1", "grid_bbox": Q1_REGION, "confidence": 0.9}],
        proposer={"contract_version": "answer-sheet-template-locator-v1", "provider_route": "qwen3.8max"},
    )
    assert skipped == []
    [row] = AnswerSheetSession.load(session.session_id, session.upload_dir).bubble_grids
    assert row["state"] == "proposed"
    assert row["cells"] == []
    assert row["source"] == "model_proposal"
    assert row["confirmed_by_teacher_id"] is None and row["confirmed_at"] is None
    assert row["proposal"]["grid_bbox"] == Q1_REGION
    assert row["proposal"]["proposer"]["provider_route"] == "qwen3.8max"

    summary = session.layout_summary()
    assert summary["unconfirmed_bubble_grids"] == ["1"]
    assert summary["missing_bubble_grids"] == ["2(1)", "2(2)"]
    assert summary["can_freeze"] is False
    with pytest.raises(ValueError):
        session.freeze_layout()
    with pytest.raises(ValueError, match="未经教师确认"):
        freeze_grid_rows(session.bubble_grids, session.answer_regions)


@pytest.mark.parametrize("claim", [
    {"teacher_confirmed": True},
    {"state": "confirmed"},
    {"confirmed_by_teacher_id": 1, "confirmed_at": "2026-09-27T00:00:00+00:00"},
    {"cells": teacher_confirmed_bubble_grid("1", Q1_REGION)["cells"]},
])
def test_a_proposal_can_never_carry_the_teachers_decision(tmp_path, claim):
    session = _draft(tmp_path)
    with pytest.raises(ValueError, match="模型提议"):
        session.propose_bubble_grids(
            [{"question_no": "1", "grid_bbox": Q1_REGION, **claim}],
            proposer={"provider_route": "qwen3.8max"},
        )
    assert AnswerSheetSession.load(session.session_id, session.upload_dir).bubble_grids == []


def test_teacher_confirm_stamps_who_and_when_and_keeps_the_proposal(tmp_path, monkeypatch):
    session = _draft(tmp_path)
    session.propose_bubble_grids(
        [{"question_no": "1", "grid_bbox": Q1_REGION}], proposer={"provider_route": "qwen3.8max"},
    )
    _set_clock(monkeypatch, datetime(2026, 9, 27, 15, 0, 0))
    session.confirm_bubble_grids([teacher_confirmed_bubble_grid("1", Q1_REGION)], teacher_id=1)

    row = AnswerSheetSession.load(session.session_id, session.upload_dir).bubble_grids[0]
    assert row["state"] == "confirmed"
    assert row["confirmed_by_teacher_id"] == 1
    assert row["confirmed_at"] == "2026-09-27T15:00:00+00:00"
    assert row["source"] == "model_proposal"
    assert row["proposal"]["grid_bbox"] == Q1_REGION
    assert row["adjusted"] is False  # accepted exactly where the model proposed
    assert [cell["option"] for cell in row["cells"]] == ["A", "B", "C", "D"]

    # Adjusting re-stamps the confirmation and records the deviation.
    _set_clock(monkeypatch, datetime(2026, 9, 27, 15, 5, 0))
    session.confirm_bubble_grids([teacher_confirmed_bubble_grid("1", [120, 210, 480, 250])], teacher_id=1)
    row = session.bubble_grids[0]
    assert row["adjusted"] is True
    assert row["grid_bbox"] == [120, 210, 480, 250]
    assert row["confirmed_at"] == "2026-09-27T15:05:00+00:00"

    # Rows the teacher draws without any proposal are attributed as such.
    session.confirm_bubble_grids(_all_rows()[1:], teacher_id=1)
    small = [row for row in session.bubble_grids if row["question_no"] == "2"]
    assert [row["subquestion_no"] for row in small] == ["1", "2"]
    assert all(row["source"] == "teacher_drawn" and row["proposal"] is None for row in small)
    assert session.layout_summary()["unconfirmed_bubble_grids"] == []


def test_a_new_proposal_never_overwrites_a_confirmed_row(tmp_path):
    session = _draft(tmp_path)
    session.confirm_bubble_grids([teacher_confirmed_bubble_grid("1", Q1_REGION)], teacher_id=1)
    confirmed = json.loads(json.dumps(session.bubble_grids))
    skipped = session.propose_bubble_grids(
        [{"question_no": "1", "grid_bbox": [150, 205, 450, 255]}], proposer={"provider_route": "qwen3.8max"},
    )
    assert skipped == ["1"]
    assert session.bubble_grids == confirmed


def test_discard_returns_a_row_to_missing(tmp_path):
    session = _draft(tmp_path)
    session.confirm_bubble_grids(_all_rows(), teacher_id=1)
    assert session.layout_summary()["can_freeze"] is True
    summary = session.discard_bubble_grids([{"question_no": "2", "subquestion_no": "1"}])
    assert summary["missing_bubble_grids"] == ["2(1)"]
    assert summary["can_freeze"] is False
    with pytest.raises(ValueError, match="没有可删除"):
        session.discard_bubble_grids([{"question_no": "2", "subquestion_no": "1"}])


def test_layout_save_keeps_rows_only_while_their_region_is_unchanged(tmp_path):
    session = _draft(tmp_path)
    session.confirm_bubble_grids(_all_rows(), teacher_id=1)
    session.save_layout(_regions(), IDENTITY)
    assert len(session.bubble_grids) == 3

    # Moving Q1's region drops its confirmation instead of inheriting it.
    session.save_layout(_regions(q1_bbox=[100, 190, 520, 270]), IDENTITY)
    assert [(row["question_no"], row["subquestion_no"]) for row in session.bubble_grids] == [
        ("2", "1"), ("2", "2"),
    ]
    assert session.layout_summary()["missing_bubble_grids"] == ["1"]


def _cells(*boxes, options="ABCD"):
    return [{"option": option, "bbox": list(box)} for option, box in zip(options, boxes)]


_ROW = [(100, 200, 200, 260), (200, 200, 300, 260), (300, 200, 400, 260), (400, 200, 500, 260)]


@pytest.mark.parametrize("request_row, message", [
    ({"question_no": "1", "cells": _cells(*_ROW[:3], (400, 200, 510, 260))}, "涂卡区域内"),
    ({"question_no": "1", "cells": _cells(*_ROW[:3], (350, 200, 500, 260))}, "重叠"),
    ({"question_no": "1", "cells": _cells(*_ROW[:3])}, "恰好覆盖"),
    ({"question_no": "1", "cells": _cells(*_ROW, options="ABCC")}, "恰好覆盖"),
    ({"question_no": "1", "cells": _cells(_ROW[1], _ROW[0], _ROW[2], _ROW[3])}, "排列顺序"),
    ({"question_no": "1", "cells": _cells(*_ROW[:3], (400, 200, 402, 260))}, "过小"),
    ({"question_no": "1", "grid_order": "diagonal", "cells": _cells(*_ROW)}, "排列方向"),
    ({"question_no": "3", "cells": _cells(*_ROW)}, "涂卡区域"),
    ({"question_no": "9", "cells": _cells(*_ROW)}, "涂卡区域"),
    ({"question_no": "1", "subquestion_no": "1", "cells": _cells(*_ROW)}, "小问结构"),
    ({"question_no": "2", "cells": _cells(*_ROW)}, "小问结构"),
    ({"question_no": "2", "subquestion_no": "3", "cells": _cells(*_ROW)}, "小问结构"),
])
def test_invalid_teacher_geometry_is_rejected_without_state_change(tmp_path, request_row, message):
    session = _draft(tmp_path)
    with pytest.raises(ValueError, match=message):
        session.confirm_bubble_grids([request_row], teacher_id=1)
    assert AnswerSheetSession.load(session.session_id, session.upload_dir).bubble_grids == []


def test_small_question_rows_of_one_card_may_not_overlap(tmp_path):
    session = _draft(tmp_path)
    with pytest.raises(ValueError, match="重叠"):
        session.confirm_bubble_grids([
            teacher_confirmed_bubble_grid("2", Q2_ROW_1, subquestion_no="1"),
            teacher_confirmed_bubble_grid("2", [100, 340, 500, 400], subquestion_no="2"),
        ], teacher_id=1)


def test_grid_editing_is_closed_outside_the_layout_draft(tmp_path):
    session = _draft(tmp_path)
    session.confirm_bubble_grids(_all_rows(), teacher_id=1)
    _freeze(session)
    assert session.status == "frozen"
    for action in (
        lambda: session.confirm_bubble_grids(_all_rows()[:1], teacher_id=1),
        lambda: session.propose_bubble_grids(
            [{"question_no": "1", "grid_bbox": Q1_REGION}], proposer={},
        ),
        lambda: session.discard_bubble_grids([{"question_no": "1"}]),
    ):
        with pytest.raises(ValueError, match="不能再修改涂卡网格"):
            action()


def test_confirm_api_stamps_the_authenticated_teacher_and_rejects_client_provenance(
    client_env,  # noqa: F811
):
    make_client, upload_dir = client_env
    session = _draft(upload_dir)
    url = f"/api/answer-sheet/sessions/{session.session_id}/layout/bubble-grids"
    row = teacher_confirmed_bubble_grid("1", Q1_REGION)
    client = make_client(1)

    for forged in (
        {"teacher_confirmed": True},
        {"confirmed_by_teacher_id": 2},
        {"confirmed_at": "2020-01-01T00:00:00+00:00"},
        {"state": "confirmed"},
    ):
        response = client.post(f"{url}/confirm", json={"grids": [{**row, **forged}]})
        assert response.status_code == 422
    assert make_client(2).post(f"{url}/confirm", json={"grids": [row]}).status_code == 404
    assert AnswerSheetSession.load(session.session_id, upload_dir).bubble_grids == []

    confirmed = make_client(1).post(f"{url}/confirm", json={"grids": [row]})
    assert confirmed.status_code == 200, confirmed.text
    [stored] = confirmed.json()["session"]["bubble_grids"]
    assert stored["state"] == "confirmed"
    assert stored["confirmed_by_teacher_id"] == 1
    assert confirmed.json()["summary"]["missing_bubble_grids"] == ["2(1)", "2(2)"]

    invalid = client.post(f"{url}/confirm", json={"grids": [{**row, "question_no": "3"}]})
    assert invalid.status_code == 409
    discarded = client.post(f"{url}/discard", json={"keys": [{"question_no": "1"}]})
    assert discarded.status_code == 200
    assert discarded.json()["session"]["bubble_grids"] == []


def test_locator_output_becomes_unconfirmed_proposals_for_batches(client_env, monkeypatch):  # noqa: F811
    from services import answer_sheet_template_locator as locator

    make_client, upload_dir = client_env
    session = _draft(upload_dir)
    response = json.dumps({
        "coordinate_space": "normalized_1000_v1",
        "column_count": 1,
        "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "answer_regions": [
            {"question_no": "1", "bbox": Q1_REGION, "confidence": 0.9},
            {"question_no": "2", "bbox": Q2_REGION, "confidence": 0.8},
            {"question_no": "3", "bbox": FILL_REGION, "confidence": 0.7},
        ],
        "identity_regions": [
            {"identity_type": "name", "bbox": [80, 60, 420, 150], "confidence": 0.95},
        ],
    })

    async def provider(_path, _prompt, **kwargs):
        kwargs["response_validator"](response)
        return response

    monkeypatch.setattr(locator, "call_gpt_vision", provider)
    located = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/layout/locate",
    )
    assert located.status_code == 200, located.text
    body = located.json()
    # Only the whole-question card gets a proposal; small-question rows are
    # never split out of one model box by guesswork.
    [row] = body["session"]["bubble_grids"]
    assert (row["question_no"], row["subquestion_no"], row["state"]) == ("1", "", "proposed")
    assert row["cells"] == [] and row["confirmed_by_teacher_id"] is None
    assert row["grid_bbox"] == Q1_REGION
    assert row["proposal"]["confidence"] == 0.9
    assert row["proposal"]["proposer"] == {
        "contract_version": locator.LOCATOR_CONTRACT_VERSION, "provider_route": "qwen3.8max",
    }
    assert body["summary"]["unconfirmed_bubble_grids"] == ["1"]
    assert body["summary"]["missing_bubble_grids"] == ["2(1)", "2(2)"]
    assert body["summary"]["can_freeze"] is False


# --- whole-question and small-question numbering ---------------------------

def test_choice_row_keys_follow_the_paper_small_question_structure():
    assert choice_row_keys({"question_no": "1"}) == [""]
    assert choice_row_keys({"question_no": "1", "subquestion_count": 1}) == [""]
    assert choice_row_keys({"question_no": "1", "subquestion_count": 3}) == ["1", "2", "3"]
    with pytest.raises(ValueError):
        choice_row_keys({"question_no": "1", "subquestion_count": 0})


def test_handwritten_choice_needs_no_grid_and_cannot_own_one():
    regions = [{
        "region_id": "answer_1", "page_id": "page_1", "question_no": "1",
        "bbox": Q1_REGION, "region_type": "choice_handwritten", "choice_options": ["A", "B", "C", "D"],
    }]
    specs = _specs()[:1]
    assert bubble_grid_coverage(regions, [], specs)["ok"] is True
    stray = {
        "question_no": "1", "subquestion_no": "", "region_id": "answer_1",
        "state": "confirmed", "confirmed_by_teacher_id": 1, "confirmed_at": "2026-09-27T00:00:00+00:00",
    }
    coverage = bubble_grid_coverage(regions, [stray], specs)
    assert coverage["ok"] is False and coverage["conflicts"]


def test_whole_and_small_questions_resolve_to_their_own_rows(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    batch = _bound_batch(tmp_path, registry, template["template_code"])

    questions = registry.resolve_bubble_regions(batch)["questions"]
    assert list(questions) == ["1", "2(1)", "2(2)"]
    assert (questions["2(1)"]["question_no"], questions["2(1)"]["subquestion_no"]) == ("2", "1")
    # Both small questions share Q2's card region but own distinct rows.
    assert questions["2(1)"]["region_id"] == questions["2(2)"]["region_id"] == "answer_2"
    assert questions["2(1)"]["grid_pixel_box"][3] <= questions["2(2)"]["grid_pixel_box"][1]
    assert questions["1"]["subquestion_no"] == ""


@pytest.mark.parametrize("q2_small_questions, reason", [(1, "2(1)"), (3, "2(3)")])
def test_small_question_structure_mismatch_fails_closed(tmp_path, q2_small_questions, reason):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    batch = _session(tmp_path / "uploads", "c" * 16, specs=_specs(q2_small_questions=q2_small_questions))
    with pytest.raises(AnswerSheetTemplateIncompatible, match=reason.replace("(", r"\(").replace(")", r"\)")):
        registry.apply_to_session(batch, template["template_code"])
    assert batch.status == "answers_confirmed"
    assert batch.answer_sheet_pages == [] and batch.shared_template_code == ""


# --- frozen-metadata integrity ----------------------------------------------

def test_publish_freezes_canonical_geometry_with_attributed_confirmations(tmp_path, monkeypatch):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _draft(tmp_path / "authoring")
    _set_clock(monkeypatch, datetime(2026, 9, 27, 15, 0, 0))
    # Confirm in a different order than the sheet order.
    source.confirm_bubble_grids(list(reversed(_all_rows())), teacher_id=1)
    _freeze(source)
    template = registry.publish(source, creator_teacher_id=1)

    manifest = json.loads(_manifest_path(registry, template["template_code"]).read_text(encoding="utf-8"))
    grids = manifest["content"]["bubble_grids"]
    assert [(row["question_no"], row["subquestion_no"]) for row in grids] == [
        ("1", ""), ("2", "1"), ("2", "2"),
    ]
    assert all(row["teacher_confirmed"] is True for row in grids)
    assert all("confirmed_by_teacher_id" not in row and "state" not in row for row in grids)
    assert grids[0]["cells"][2] == {"option": "C", "bbox": [300, 200, 400, 260], "cell_size": [100, 60]}
    record = manifest["bubble_grid_confirmations"]
    assert record["fingerprint"] == manifest["fingerprint"]
    assert [(row["question_no"], row["subquestion_no"]) for row in record["rows"]] == [
        ("1", ""), ("2", "1"), ("2", "2"),
    ]
    assert {row["confirmed_by_teacher_id"] for row in record["rows"]} == {1}
    assert {row["confirmed_at"] for row in record["rows"]} == {"2026-09-27T15:00:00+00:00"}
    assert template["bubble_grid_count"] == 3
    # No paper-template content leaks into the shared layout template.
    text = json.dumps(manifest, ensure_ascii=False)
    assert "standard_answer" not in text and "subquestion_count" not in text


def test_publish_refuses_a_historical_layout_with_a_pending_proposal(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _draft(tmp_path / "authoring", batch=False)
    source.propose_bubble_grids(
        [{"question_no": "1", "grid_bbox": Q1_REGION}], proposer={"provider_route": "qwen3.8max"},
    )
    assert source.layout_summary()["unconfirmed_bubble_grids"] == ["1"]
    with pytest.raises(ValueError, match="尚未完成教师确认"):
        registry.publish(source, creator_teacher_id=1)
    assert not registry.templates_dir.exists() or not any(registry.templates_dir.iterdir())


def test_same_geometry_confirmed_by_another_teacher_later_keeps_the_same_code(tmp_path, monkeypatch):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _set_clock(monkeypatch, datetime(2026, 9, 27, 15, 0, 0))
    _first, first = _published(tmp_path / "one", registry, "a" * 16, teacher_id=1)
    _set_clock(monkeypatch, datetime(2026, 9, 28, 9, 0, 0))
    _second, second = _published(tmp_path / "two", registry, "c" * 16, teacher_id=2)
    assert second["template_code"] == first["template_code"]
    manifest = json.loads(_manifest_path(registry, first["template_code"]).read_text(encoding="utf-8"))
    # The original confirmation record stays authoritative.
    assert {row["confirmed_by_teacher_id"] for row in manifest["bubble_grid_confirmations"]["rows"]} == {1}


def _tamper_cell(manifest):
    manifest["content"]["bubble_grids"][0]["cells"][0]["bbox"][0] += 1


def _tamper_confirmed_flag(manifest):
    manifest["content"]["bubble_grids"][0]["teacher_confirmed"] = False


def _tamper_confirmer(manifest):
    manifest["bubble_grid_confirmations"]["rows"][0]["confirmed_by_teacher_id"] = 2


def _drop_confirmations(manifest):
    del manifest["bubble_grid_confirmations"]


def _rehash_invalid_confirmation(manifest):
    rows = manifest["bubble_grid_confirmations"]["rows"]
    rows[0]["confirmed_at"] = ""
    manifest["bubble_grid_confirmations"] = registry_module._grid_confirmation_record(
        manifest["fingerprint"], rows,
    )


def _rehash_foreign_fingerprint(manifest):
    manifest["bubble_grid_confirmations"] = registry_module._grid_confirmation_record(
        "0" * 64, manifest["bubble_grid_confirmations"]["rows"],
    )


@pytest.mark.parametrize("tamper", [
    _tamper_cell,
    _tamper_confirmed_flag,
    _tamper_confirmer,
    _drop_confirmations,
    _rehash_invalid_confirmation,
    _rehash_foreign_fingerprint,
])
def test_tampered_frozen_grid_metadata_is_rejected(tmp_path, tamper):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    code = template["template_code"]
    path = _manifest_path(registry, code)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tamper(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="完整性"):
        registry._load_manifest(code)
    batch = _session(tmp_path / "uploads", "d" * 16)
    with pytest.raises(ValueError):
        registry.apply_to_session(batch, code)
    assert batch.status == "answers_confirmed"


def test_self_consistent_manifest_with_an_unconfirmed_grid_is_rejected(tmp_path):
    """Even a correctly re-hashed template cannot carry an unconfirmed row."""
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    original_dir = registry.templates_dir / template["template_code"]
    manifest = json.loads((original_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["content"]["bubble_grids"][0]["teacher_confirmed"] = False
    fingerprint = hashlib.sha256(
        registry_module._stable_json(manifest["content"]).encode("utf-8"),
    ).hexdigest()
    code = registry_module._template_code(fingerprint)
    manifest.update(template_code=code, fingerprint=fingerprint)
    manifest["bubble_grid_confirmations"] = registry_module._grid_confirmation_record(
        fingerprint, manifest["bubble_grid_confirmations"]["rows"],
    )
    forged_dir = registry.templates_dir / code
    (forged_dir / "pages").mkdir(parents=True)
    for asset in (original_dir / "pages").iterdir():
        (forged_dir / "pages" / asset.name).write_bytes(asset.read_bytes())
    (forged_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="未经教师确认"):
        registry._load_manifest(code)


# --- apply_to_session geometry contract ---------------------------------------

def test_apply_stores_no_grid_in_the_session_and_resolves_pixels_from_the_template(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    _source, template = _published(tmp_path, registry)
    batch = _bound_batch(tmp_path, registry, template["template_code"])

    assert batch.status == "frozen"
    assert batch.bubble_grids == []
    meta = json.loads(Path(batch.meta_path).read_text(encoding="utf-8"))
    assert meta["bubble_grids"] == []
    assert meta["shared_template_code"] == template["template_code"]

    resolved = registry.resolve_bubble_regions(batch)
    assert resolved["template_code"] == template["template_code"]
    assert resolved["template_fingerprint"] == batch.shared_template_hash
    q1 = resolved["questions"]["1"]
    assert q1["page_size"] == list(PAGE_SIZE)
    assert [cell["option"] for cell in q1["cells"]] == ["A", "B", "C", "D"]
    # 800x1200 page: x = floor/ceil(n * 800 / 1000), y = floor/ceil(n * 1200 / 1000).
    assert q1["cells"][0]["pixel_box"] == [80, 240, 160, 312]
    assert q1["cells"][3]["pixel_box"] == [320, 240, 400, 312]
    assert q1["grid_pixel_box"] == [80, 240, 400, 312]

    # The resolved boxes are exactly the pixels the recognition crop reads.
    height, width = PAGE_SIZE[1], PAGE_SIZE[0]
    page = np.arange(height * width, dtype=np.int64).reshape(height, width)[..., None].repeat(3, axis=2)
    for entry in resolved["questions"].values():
        for cell in entry["cells"]:
            x1, y1, x2, y2 = cell["pixel_box"]
            assert np.array_equal(crop_normalized_rgb(page, cell["bbox"]), page[y1:y2, x1:x2])


@pytest.mark.parametrize("size", [(800, 1200), (2064, 1564), (997, 1001), (333, 777)])
def test_pixel_rounding_matches_the_recognition_crop(size):
    width, height = size
    page = np.arange(height * width, dtype=np.int64).reshape(height, width)[..., None].repeat(3, axis=2)
    rng = random.Random(width * 7 + height)
    boxes = [[0, 0, 1000, 1000], [0, 0, 1, 1], [999, 999, 1000, 1000]]
    for _ in range(200):
        x1, x2 = sorted(rng.sample(range(1001), 2))
        y1, y2 = sorted(rng.sample(range(1001), 2))
        boxes.append([x1, y1, x2, y2])
    for bbox in boxes:
        x1, y1, x2, y2 = normalized_bbox_to_pixels(bbox, width, height)
        assert np.array_equal(crop_normalized_rgb(page, bbox), page[y1:y2, x1:x2]), bbox


def test_template_without_confirmed_grids_cannot_bind_a_batch(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    legacy = _draft(tmp_path / "authoring", batch=False)
    _freeze(legacy)
    template = registry.publish(legacy, creator_teacher_id=1)
    assert template["bubble_grid_count"] == 0
    batch = _session(tmp_path / "uploads", "e" * 16)
    with pytest.raises(AnswerSheetTemplateIncompatible, match="缺少涂卡网格"):
        registry.apply_to_session(batch, template["template_code"])
    assert batch.status == "answers_confirmed"


# --- fail-closed rejection at grading time -----------------------------------

def test_grading_gate_resolves_only_a_confirmed_bound_template(tmp_path, monkeypatch):
    from routers import answer_sheet

    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    monkeypatch.setattr(answer_sheet, "get_answer_sheet_template_registry", lambda: registry)

    # Frozen with confirmed draft rows but never published: not gradable.
    unpublished = _draft(tmp_path / "authoring", "f" * 16)
    unpublished.confirm_bubble_grids(_all_rows(), teacher_id=1)
    _freeze(unpublished)
    with pytest.raises(HTTPException) as caught:
        answer_sheet._verified_bubble_grids(unpublished)
    assert caught.value.status_code == 409
    assert "未绑定" in caught.value.detail

    _source, template = _published(tmp_path, registry)
    batch = _bound_batch(tmp_path, registry, template["template_code"])
    assert list(answer_sheet._verified_bubble_grids(batch)["questions"]) == ["1", "2(1)", "2(2)"]

    diverged_hash = AnswerSheetSession.load(batch.session_id, batch.upload_dir)
    diverged_hash.shared_template_hash = "0" * 64
    diverged_regions = AnswerSheetSession.load(batch.session_id, batch.upload_dir)
    diverged_regions.answer_regions[0]["bbox"] = [100, 190, 520, 270]
    for session in (diverged_hash, diverged_regions):
        with pytest.raises(HTTPException) as caught:
            answer_sheet._verified_bubble_grids(session)
        assert caught.value.status_code == 409

    # A template changed on disk after binding is rejected at grading time.
    path = _manifest_path(registry, template["template_code"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    _tamper_confirmer(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HTTPException) as caught:
        answer_sheet._verified_bubble_grids(AnswerSheetSession.load(batch.session_id, batch.upload_dir))
    assert caught.value.status_code == 409


def test_grading_gate_rejects_a_batch_bound_to_a_template_without_grids(tmp_path, monkeypatch):
    from routers import answer_sheet

    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    monkeypatch.setattr(answer_sheet, "get_answer_sheet_template_registry", lambda: registry)
    legacy = _draft(tmp_path / "authoring", batch=False)
    _freeze(legacy)
    template = registry.publish(legacy, creator_teacher_id=1)
    # A batch forced onto the grid-less layout (bypassing apply) still fails.
    batch = _session(tmp_path / "uploads", "e" * 16)
    batch.answer_regions = json.loads(json.dumps(legacy.answer_regions))
    batch.shared_template_code = template["template_code"]
    batch.shared_template_hash = json.loads(
        _manifest_path(registry, template["template_code"]).read_text(encoding="utf-8"),
    )["fingerprint"]
    with pytest.raises(HTTPException) as caught:
        answer_sheet._verified_bubble_grids(batch)
    assert caught.value.status_code == 409
    assert "缺少涂卡网格" in caught.value.detail

    # Historical sessions keep their legacy behavior and are not gated here.
    assert answer_sheet._verified_bubble_grids(legacy) is None


def _review_ready(upload_dir, *, with_drafts: bool) -> AnswerSheetSession:
    session = _draft(upload_dir, "9" * 16)
    session.confirm_bubble_grids(_all_rows(), teacher_id=1)
    _freeze(session)
    session.status = "review"
    session.student_pages = [{
        "paper_id": "paper_1",
        "orientation_status": "teacher_confirmed",
        "registration": {"status": "passed", "diagnostics": {"answer_source_complete": True}},
    }]
    session.grouping_decisions = [{"student_id": 101}] if with_drafts else []
    session.save()
    return session


def test_grading_endpoints_fail_closed_before_any_model_call(client_env, tmp_path, monkeypatch):  # noqa: F811
    import services.grader as grader

    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("grading must not start without confirmed bubble grids")

    monkeypatch.setattr(grader, "grade_paper_unified", forbidden)
    client = make_client(1)

    session = _review_ready(upload_dir, with_drafts=False)
    base = f"/api/answer-sheet/sessions/{session.session_id}"
    draft = client.post(f"{base}/grading/draft")
    assert draft.status_code == 409
    assert "涂卡网格" in draft.json()["detail"]

    stream = client.post(f"{base}/grading/draft/stream")
    assert "ANSWER_SHEET_VALIDATION_FAILED" in stream.text
    assert "涂卡网格" in stream.text

    session.grouping_decisions = [{"student_id": 101}]
    session.save()
    archived = client.post(f"{base}/grading/archive")
    assert archived.status_code == 409
    assert "涂卡网格" in archived.json()["detail"]
    assert AnswerSheetSession.load(session.session_id, upload_dir).status == "review"


def test_publish_endpoint_moves_grids_to_the_template_and_keeps_no_copy(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    _paper_registry, sheet_registry = install_registries(monkeypatch, tmp_path)
    session = _draft(upload_dir)
    session.confirm_bubble_grids(_all_rows(), teacher_id=1)
    _freeze(session)

    published = make_client(1).post(
        f"/api/answer-sheet/sessions/{session.session_id}/layout/templates/publish",
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["session"]["bubble_grids"] == []
    assert body["template"]["bubble_grid_count"] == 3
    reloaded = AnswerSheetSession.load(session.session_id, upload_dir)
    assert reloaded.shared_template_code == body["template"]["template_code"]
    assert reloaded.status == "frozen"
    assert reloaded.layout_summary()["bubble_grid_source"] == "template"
    assert list(answer_sheet._verified_bubble_grids(reloaded)["questions"]) == ["1", "2(1)", "2(2)"]

    # Re-publishing the bound batch returns the same code, never a grid-less one.
    again = sheet_registry.publish(reloaded, creator_teacher_id=1)
    assert again["template_code"] == body["template"]["template_code"]
    assert len(list(sheet_registry.templates_dir.iterdir())) == 1
