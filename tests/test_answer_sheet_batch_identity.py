"""Slice 1: batch identity = answer-sheet template code + timestamp.

The class comes from the teacher's global current class and the historical
daily/exam ``category`` is neither chosen nor written for new batches.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from services import answer_sheet_session as session_module
from services.answer_sheet_session import (
    BATCH_CONTRACT_VERSION,
    BATCH_INDEX_NAME,
    AnswerSheetSession,
    list_teacher_batches,
    make_batch_label,
)
from tests.answer_sheet_redesign_helpers import (
    confirmed_detailed_template,
    install_registries,
    register_paper,
    teacher_confirmed_bubble_grid,
)
from tests.test_answer_sheet_session import client_env  # noqa: F401
from tests.test_answer_sheet_template_codes import _frozen_session


LABEL_RE = re.compile(r"^AS-[0-9A-F]{4}(?:-[0-9A-F]{4}){3}-\d{8}-\d{6}$")
_SINGLE_CHOICE = [{
    "question_no": "1",
    "question_text": "第1题",
    "question_type": "choice",
    "choice_mode": "single",
    "standard_answer": "A",
}]


def _codes(client, upload_dir: str, tmp_path: Path, monkeypatch) -> tuple[str, str]:
    """A one-question paper template and a matching published layout code."""
    _paper_registry, sheet_registry = install_registries(monkeypatch, tmp_path)
    confirmed_detailed_template(
        upload_dir, "label-paper", category="daily", questions=_SINGLE_CHOICE,
    )
    paper_code = register_paper(client, "label-paper")["template_code"]
    # Batches judge choice marks locally, so the published layout carries the
    # teacher-confirmed bubble grid for its choice_mark region.
    source = _frozen_session(
        tmp_path / "template-source",
        "9" * 16,
        bubble_grids=[teacher_confirmed_bubble_grid("1", [80, 300, 920, 520])],
    )
    sheet_code = sheet_registry.publish(source, creator_teacher_id=1)["template_code"]
    return paper_code, sheet_code


def _create(client, paper_code: str, sheet_code: str | None = None):
    body = {"paper_template_code": paper_code}
    if sheet_code:
        body["answer_sheet_template_code"] = sheet_code
    return client.post("/api/answer-sheet/sessions", json=body)


def test_batch_label_is_template_code_plus_second_timestamp():
    label = make_batch_label(
        "AS-7K2M-0000-0000-0000", datetime(2026, 9, 27, 14, 30, 5),
    )
    assert label == "AS-7K2M-0000-0000-0000-20260927-143005"
    with pytest.raises(ValueError):
        make_batch_label("", datetime(2026, 9, 27))


def test_new_batch_inherits_global_class_and_never_writes_category(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, _sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)
    created = _create(client, paper_code)
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["class_id"] == 10
    assert payload["batch_contract_version"] == BATCH_CONTRACT_VERSION
    assert payload["batch_label"] == ""
    assert payload["paper_template_code"] == paper_code
    assert "category" not in payload

    meta = json.loads(
        (Path(upload_dir) / f"answer_sheet_{payload['session_id']}" / "meta.json")
        .read_text(encoding="utf-8")
    )
    assert "category" not in meta
    assert meta["class_id"] == 10


@pytest.mark.parametrize("extra", [
    {"category": "daily"},
    {"category": "exam", "exam_name": "月考"},
    {"class_id": 10},
    {"exam_name": "月考"},
])
def test_batch_creation_rejects_per_batch_class_and_grading_type(client_env, extra):  # noqa: F811
    make_client, upload_dir = client_env
    response = make_client(1).post(
        "/api/answer-sheet/sessions",
        json={"paper_template_code": "PT-0000-0000-0000-0000", **extra},
    )
    assert response.status_code == 422
    assert not list(Path(upload_dir).glob("answer_sheet_*"))


def test_batch_creation_requires_a_selected_global_class(client_env):  # noqa: F811
    from models_user import User
    from routers.auth import _require_user

    make_client, _ = client_env
    client = make_client(1)
    client.app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="t", role="teacher", education_stage="senior", current_class_id=None,
    )
    response = _create(client, "PT-0000-0000-0000-0000")
    assert response.status_code == 400
    assert "当前班级" in response.json()["detail"]


def test_batch_creation_rejects_a_global_class_owned_by_another_teacher(client_env):  # noqa: F811
    from models_user import User
    from routers.auth import _require_user

    make_client, _ = client_env
    client = make_client(1)
    client.app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="t", role="teacher", education_stage="senior", current_class_id=20,
    )
    assert _create(client, "PT-0000-0000-0000-0000").status_code == 404


def test_selected_template_code_labels_the_batch_at_creation(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)

    created = _create(client, paper_code, sheet_code)
    assert created.status_code == 200, created.text
    batch = created.json()
    assert batch["status"] == "frozen"
    assert batch["answer_sheet_template_code"] == sheet_code
    assert LABEL_RE.fullmatch(batch["batch_label"])
    assert batch["batch_label"].startswith(f"{sheet_code}-")

    listed = client.get("/api/answer-sheet/sessions").json()["batches"]
    assert [item["session_id"] for item in listed] == [batch["session_id"]]
    assert listed[0]["batch_label"] == batch["batch_label"]
    assert listed[0]["paper_template_code"] == paper_code
    assert listed[0]["status"] == "frozen"
    assert "teacher_id" not in listed[0]
    assert make_client(2).get("/api/answer-sheet/sessions").json()["batches"] == []


def test_template_use_after_creation_assigns_the_label(client_env, tmp_path, monkeypatch):  # noqa: F811
    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)
    batch = _create(client, paper_code).json()
    used = client.post(
        f"/api/answer-sheet/sessions/{batch['session_id']}/layout/templates/use",
        json={"template_code": sheet_code},
    )
    assert used.status_code == 200, used.text
    session = used.json()["session"]
    assert session["answer_sheet_template_code"] == sheet_code
    assert LABEL_RE.fullmatch(session["batch_label"])


def test_same_template_same_second_cannot_share_a_label(client_env, tmp_path, monkeypatch):  # noqa: F811
    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 27, 14, 30, 0, tzinfo=tz)

    monkeypatch.setattr(session_module, "datetime", FrozenClock)
    first = _create(client, paper_code, sheet_code)
    assert first.status_code == 200
    assert first.json()["batch_label"] == f"{sheet_code}-20260927-143000"

    duplicate = _create(client, paper_code, sheet_code)
    assert duplicate.status_code == 409
    # The rejected batch leaves no directory or listing row behind.
    assert [item["session_id"] for item in list_teacher_batches(upload_dir, 1)] == [
        first.json()["session_id"]
    ]

    pending = _create(client, paper_code).json()
    used = client.post(
        f"/api/answer-sheet/sessions/{pending['session_id']}/layout/templates/use",
        json={"template_code": sheet_code},
    )
    assert used.status_code == 409
    reloaded = AnswerSheetSession.load(pending["session_id"], upload_dir)
    assert reloaded.batch_label == ""
    assert reloaded.status == "answers_confirmed"


def test_simultaneous_same_template_batches_cannot_both_claim_a_label(
    client_env, tmp_path, monkeypatch,  # noqa: F811
):
    from routers import answer_sheet_sessions

    make_client, upload_dir = client_env
    client = make_client(1)
    paper_code, sheet_code = _codes(client, upload_dir, tmp_path, monkeypatch)
    # The registry the batch-creating route uses (the fixture patches its readers).
    sheet_registry = answer_sheet_sessions.get_answer_sheet_template_registry()
    original_apply = sheet_registry.apply_to_session

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 27, 14, 30, 0, tzinfo=tz)

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def apply_held_before_save(session, code):
        # Hold the first request after its uniqueness scan but before its
        # batch index is saved; without the label lock the second would pass.
        calls.append(code)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=10)
        return original_apply(session, code)

    monkeypatch.setattr(session_module, "datetime", FrozenClock)
    monkeypatch.setattr(sheet_registry, "apply_to_session", apply_held_before_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_first = pool.submit(_create, client, paper_code, sheet_code)
        try:
            assert entered.wait(timeout=10)
            second = _create(make_client(1), paper_code, sheet_code)
        finally:
            release.set()
        first = pending_first.result(timeout=30)

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert [item["batch_label"] for item in list_teacher_batches(upload_dir, 1)] == [
        f"{sheet_code}-20260927-143000"
    ]
    assert len(list(Path(upload_dir).glob("answer_sheet_*"))) == 1

    # The lock is released with the request: a later second binds normally.
    monkeypatch.setattr(session_module, "datetime", datetime)
    later = _create(client, paper_code, sheet_code)
    assert later.status_code == 200, later.text


def test_historical_category_is_read_only_and_never_backfilled(tmp_path):
    upload_dir = str(tmp_path / "uploads")
    legacy_dir = Path(upload_dir) / "answer_sheet_abcdefabcdef1234"
    legacy_dir.mkdir(parents=True)
    legacy_meta = {
        "session_id": "abcdefabcdef1234",
        "teacher_id": 1,
        "class_id": 10,
        "status": "review",
        "category": "exam",
        "exam_name": "期中考试",
    }
    (legacy_dir / "meta.json").write_text(json.dumps(legacy_meta), encoding="utf-8")

    legacy = AnswerSheetSession.load("abcdefabcdef1234", upload_dir)
    assert legacy.category == "exam"
    assert legacy.result_category() == "exam"
    assert legacy.result_exam_name() == "期中考试"
    assert not legacy.is_batch
    assert legacy.to_dict(public=True)["category"] == "exam"
    legacy.save()
    saved = json.loads((legacy_dir / "meta.json").read_text(encoding="utf-8"))
    assert saved["category"] == "exam"
    assert saved["batch_label"] == ""
    # Historical sessions are not given a template-code label or listing row.
    assert not (legacy_dir / BATCH_INDEX_NAME).exists()
    assert list_teacher_batches(upload_dir, 1) == []
    with pytest.raises(ValueError):
        legacy.bind_batch_label("AS-0000-0000-0000-0000")
    with pytest.raises(ValueError):
        legacy.bind_paper_template("PT-0000-0000-0000-0000", [{"question_no": "1"}], "a" * 64)


def test_new_batch_session_carries_no_category_value(tmp_path):
    session = AnswerSheetSession("c" * 16, str(tmp_path), teacher_id=1, class_id=10)
    session.batch_contract_version = BATCH_CONTRACT_VERSION
    session.save()
    assert session.category == ""
    assert "category" not in session.to_dict()
    record = json.loads(Path(session.batch_index_path).read_text(encoding="utf-8"))
    assert "category" not in record
    assert record["teacher_id"] == 1
