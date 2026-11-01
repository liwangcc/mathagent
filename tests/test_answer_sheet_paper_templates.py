"""Slice 2: reusable paper templates frozen from the detailed-grading chain."""
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from services.answer_sheet_paper_templates import (
    PaperTemplateRejected,
    detailed_template_contract,
    paper_answer_spec_hash,
)
from services.answer_sheet_session import AnswerSheetSession
from services.template_session import TemplateSession
from tests.answer_sheet_redesign_helpers import (
    confirmed_detailed_template,
    install_registries,
    paper_questions,
    register_paper,
)
from tests.test_answer_sheet_session import client_env  # noqa: F401


CODE_RE = re.compile(r"^PT-[0-9A-F]{4}(?:-[0-9A-F]{4}){3}$")


def _create_batch(client, code: str) -> dict:
    response = client.post("/api/answer-sheet/sessions", json={"paper_template_code": code})
    assert response.status_code == 200, response.text
    return response.json()


def test_register_is_idempotent_frozen_and_private(client_env, tmp_path, monkeypatch):  # noqa: F811
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir)
    client = make_client(1)

    sources = client.get("/api/answer-sheet/paper-templates/sources").json()["sources"]
    assert [item["template_id"] for item in sources] == ["paper-src-1"]
    assert sources[0]["question_count"] == 4

    first = register_paper(client, alias="高一期中卷A")
    again = register_paper(client)
    assert CODE_RE.fullmatch(first["template_code"])
    assert again["template_code"] == first["template_code"]
    assert again["answer_spec_hash"] == first["answer_spec_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", first["answer_spec_hash"])
    assert first["alias"] == "高一期中卷A"
    assert again["alias"] == "高一期中卷A"
    assert first["scoring_category"] == "exam"
    assert first["question_counts"] == {"choice": 2, "fill": 1, "solve": 1}

    detail = client.get(f"/api/answer-sheet/paper-templates/{first['template_code']}")
    assert detail.status_code == 200
    assert [item["question_no"] for item in detail.json()["questions"]] == ["1", "2", "3", "4"]
    # Standard answers are private to the owning teacher.
    other = make_client(2)
    assert other.get(f"/api/answer-sheet/paper-templates/{first['template_code']}").status_code == 404
    assert other.get("/api/answer-sheet/paper-templates").json()["templates"] == []
    assert other.post(
        "/api/answer-sheet/paper-templates", json={"template_id": "paper-src-1"},
    ).status_code == 404


def test_one_confirmation_is_reused_by_two_batches_with_a_stable_hash(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir)
    client = make_client(1)
    template = register_paper(client)

    first = _create_batch(client, template["template_code"])
    second = _create_batch(client, template["template_code"])
    assert first["session_id"] != second["session_id"]
    for batch in (first, second):
        assert batch["status"] == "answers_confirmed"
        assert batch["paper_template_code"] == template["template_code"]
        assert batch["answer_spec_hash"] == template["answer_spec_hash"]
        assert "category" not in batch
    assert first["answer_spec_snapshot"] == second["answer_spec_snapshot"]

    # Editing the detailed-grading source after registration does not move
    # the frozen template or its batches.
    source = TemplateSession.load_meta("paper-src-1", upload_dir)
    source.pages[0]["questions"][0]["standard_answer"] = "D"
    source.save_meta()
    third = _create_batch(client, template["template_code"])
    assert third["answer_spec_hash"] == template["answer_spec_hash"]
    assert third["answer_spec_snapshot"][0]["standard_answer"] == "C"


def test_paper_and_sheet_templates_are_reused_by_two_frozen_batches(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    from datetime import datetime

    from routers import answer_sheet
    from services import answer_sheet_session as session_module
    from tests.test_answer_sheet_batch_identity import _codes

    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)
    registered = client.get(f"/api/answer-sheet/paper-templates/{paper_code}").json()["template"]
    clock = {"now": datetime(2026, 9, 27, 14, 30, 0)}

    class SettableClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].replace(tzinfo=tz)

    monkeypatch.setattr(session_module, "datetime", SettableClock)
    batches = []
    for minute in (30, 31):
        clock["now"] = datetime(2026, 9, 27, 14, minute, 0)
        response = client.post(
            "/api/answer-sheet/sessions",
            json={"paper_template_code": paper_code, "answer_sheet_template_code": sheet_code},
        )
        assert response.status_code == 200, response.text
        batches.append(response.json())

    first, second = batches
    assert first["batch_label"] == f"{sheet_code}-20260927-143000"
    assert second["batch_label"] == f"{sheet_code}-20260927-143100"
    for batch in batches:
        assert batch["status"] == "frozen"
        assert batch["paper_template_code"] == paper_code
        assert batch["answer_sheet_template_code"] == sheet_code
        assert batch["answer_spec_hash"] == registered["answer_spec_hash"]
        session = AnswerSheetSession.load(batch["session_id"], upload_dir)
        assert answer_sheet._verified_paper_template(session)["answer_spec_hash"] == (
            registered["answer_spec_hash"]
        )
    assert first["answer_spec_snapshot"] == second["answer_spec_snapshot"]
    listed = client.get("/api/answer-sheet/sessions").json()["batches"]
    assert sorted(item["batch_label"] for item in listed) == sorted(
        batch["batch_label"] for batch in batches
    )
    reloaded = client.get(f"/api/answer-sheet/paper-templates/{paper_code}").json()["template"]
    assert reloaded["answer_spec_hash"] == registered["answer_spec_hash"]
    assert reloaded.get("last_used_at")


def test_unconfirmed_stale_or_drawing_sources_are_rejected(client_env, tmp_path, monkeypatch):  # noqa: F811
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    client = make_client(1)

    draft = confirmed_detailed_template(upload_dir, "paper-draft")
    draft.status = "ocr_done"
    draft.save_meta()
    assert client.post(
        "/api/answer-sheet/paper-templates", json={"template_id": "paper-draft"},
    ).status_code == 409

    stale = confirmed_detailed_template(upload_dir, "paper-stale")
    stale.pages[0]["questions"][2]["standard_answer"] = "0.5"
    stale.save_meta()
    rejected = client.post(
        "/api/answer-sheet/paper-templates", json={"template_id": "paper-stale"},
    )
    assert rejected.status_code == 409
    assert "重新确认" in rejected.json()["detail"]

    drawing = paper_questions()[:1] + [{
        "question_no": "2",
        "question_text": "作出函数图像",
        "question_type": "solve",
        "standard_answer": "图像过原点",
        "answer_kind": "drawing",
        "blank_count": 1,
        "answer_slots": [""],
        "region_kinds": ["drawing"],
        "slot_ids": ["2[1]"],
    }]
    drawing_source = confirmed_detailed_template(upload_dir, "paper-drawing", questions=drawing)
    with pytest.raises(PaperTemplateRejected, match="作图题"):
        detailed_template_contract(drawing_source)
    assert client.post(
        "/api/answer-sheet/paper-templates", json={"template_id": "paper-drawing"},
    ).status_code == 409
    assert client.post(
        "/api/answer-sheet/paper-templates", json={"template_id": "missing"},
    ).status_code == 404


def test_batch_creation_fails_closed_for_unknown_or_foreign_paper_templates(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir)
    template = register_paper(make_client(1))

    assert make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": "PT-0000-0000-0000-0000"},
    ).status_code == 404
    assert make_client(2).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": template["template_code"]},
    ).status_code == 404
    assert not [
        path for path in Path(upload_dir).glob("answer_sheet_*")
    ]


def test_batch_creation_rejects_a_paper_template_from_another_stage(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir, "paper-junior", stage="junior")
    client = make_client(1)
    template = register_paper(client, "paper-junior")
    response = client.post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": template["template_code"]},
    )
    assert response.status_code == 409
    assert "学段" in response.json()["detail"]


def test_grading_gate_rejects_a_snapshot_that_diverged_from_its_paper_template(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    from routers import answer_sheet

    make_client, upload_dir = client_env
    paper_registry, _sheet_registry = install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir)
    client = make_client(1)
    template = register_paper(client)
    batch = _create_batch(client, template["template_code"])

    session = AnswerSheetSession.load(batch["session_id"], upload_dir)
    assert answer_sheet._verified_paper_template(session)["template_code"] == template["template_code"]
    assert answer_sheet._result_scoring(session) == ("exam", "期中考试")

    session.answer_spec_snapshot[0]["standard_answer"] = "D"
    with pytest.raises(HTTPException) as caught:
        answer_sheet._verified_paper_template(session)
    assert caught.value.status_code == 409
    assert "answer_spec_hash" in caught.value.detail

    # A tampered registry manifest is rejected by its integrity check too.
    restored = AnswerSheetSession.load(batch["session_id"], upload_dir)
    manifest_path = next((tmp_path / "paper-registry").rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["content"]["questions"][0]["standard_answer"] = "D"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HTTPException) as tampered:
        answer_sheet._verified_paper_template(restored)
    assert tampered.value.status_code == 409


def test_daily_paper_template_scores_without_points(client_env, tmp_path, monkeypatch):  # noqa: F811
    from routers import answer_sheet

    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir, "paper-daily", category="daily")
    client = make_client(1)
    template = register_paper(client, "paper-daily")
    assert template["scoring_category"] == "daily"
    batch = _create_batch(client, template["template_code"])
    session = AnswerSheetSession.load(batch["session_id"], upload_dir)
    assert answer_sheet._result_scoring(session) == ("daily", "")
    assert all(item.get("max_score") is None for item in session.answer_spec_snapshot)


def test_paper_template_alias_is_editable_and_validated(client_env, tmp_path, monkeypatch):  # noqa: F811
    make_client, upload_dir = client_env
    install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(upload_dir)
    client = make_client(1)
    template = register_paper(client)
    renamed = client.put(
        f"/api/answer-sheet/paper-templates/{template['template_code']}/alias",
        json={"alias": "  一中 高一  期中 "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["template"]["alias"] == "一中 高一 期中"
    listed = client.get("/api/answer-sheet/paper-templates").json()["templates"]
    assert listed[0]["alias"] == "一中 高一 期中"
    assert client.put(
        f"/api/answer-sheet/paper-templates/{template['template_code']}/alias",
        json={"alias": "x" * 41},
    ).status_code == 422


def test_answer_spec_hash_covers_questions_stage_and_scoring():
    questions = [{"question_no": "1", "standard_answer": "C"}]
    base = paper_answer_spec_hash(questions, {"education_stage": "senior"}, "exam")
    assert base == paper_answer_spec_hash(list(questions), {"education_stage": "senior"}, "exam")
    assert base != paper_answer_spec_hash(
        [{"question_no": "1", "standard_answer": "D"}], {"education_stage": "senior"}, "exam",
    )
    assert base != paper_answer_spec_hash(questions, {"education_stage": "junior"}, "exam")
    assert base != paper_answer_spec_hash(questions, {"education_stage": "senior"}, "daily")
