"""Golden-transcript characterization of the student recognition SSE streams.

``POST /batch-grade/recognize`` and ``POST /batch-grade/{id}/recognize/resume``
were written as two ~400-line generators and the public history has a single
import commit for ``routers/grade.py``.  Before they are split these tests pin
what a client and the next request can observe, byte for byte:

* the complete SSE transcript (event names and payloads, in order, keys in
  wire order);
* the raw persisted ``meta.json`` after the stream ends;
* one ordered ledger of every checkpoint written on the way (status, error,
  OCR job ledger, counts) interleaved with each batch/template lock acquire,
  release and busy refusal, so "checkpoint while still holding the lock" is
  pinned as well;
* the request-local context every OCR call ran under (vision phase / retry
  scope / batch size / route ordinal and the grading teacher).

Only external boundaries are stubbed (OCR provider, identity crop, grading
model, database session, batch lock); every internal helper runs for real so
that a refactor is free to restructure them.  Thread offloads run inline and
the dispatch windows are fixed, which makes the event-loop interleaving of
OCR, grading and checkpoints reproducible on any machine.  Regenerate a golden only for an intended behaviour change:

    MATHAGENT_UPDATE_GOLDEN=1 pytest tests/test_recognition_stream_characterization.py
"""
import asyncio
import copy
import json
import os
import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from models_user import User
from services import analyzer, ocr_service
from services.quick_grader import BatchOperationBusy, QuickBatch, batch_operation_lock
from tests.test_batch_calibration_flow import _events, _setup


GOLDEN_DIR = Path(__file__).parent / "golden" / "recognition_stream"
UPDATE_GOLDEN = os.environ.get("MATHAGENT_UPDATE_GOLDEN") == "1"
TEMPLATE_ID = "characterization-template"
# Checkpoints and lock transitions of one test, in the order they happened.
_LEDGER: list[list] = []


# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def deterministic_runtime(monkeypatch):
    from services import task_queue
    import routers.grade as grade

    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def no_report(*_args):
        return None

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(task_queue, "_grading_queue", None)
    monkeypatch.setattr(grade, "_start_batch_report_generation", no_report)
    # These come from the environment; the interleaving depends on them.
    monkeypatch.setattr(grade, "OCR_BATCH_DISPATCH_CONCURRENT", 3)
    monkeypatch.setattr(grade, "OCR_TEACHER_WAVE_SIZE", 3)
    monkeypatch.setattr(grade, "OCR_PREPROCESS_LOCAL_MAX_CONCURRENT", 2)


@pytest.fixture(autouse=True)
def record_ledger(monkeypatch):
    """Record each persisted snapshot and each lock transition, in order."""
    import routers.grade as grade

    _LEDGER.clear()
    save_meta = QuickBatch.save_meta
    real_lock = grade.batch_operation_lock

    def recording_save_meta(self, *, snapshot=None):
        data = json.loads(snapshot if snapshot is not None else self.serialize_meta())
        _LEDGER.append(["checkpoint", {
            "status": data.get("status"),
            "error": data.get("error"),
            "resume_generation": data.get("recognition_resume_generation"),
            "jobs": [[job.get("status"), job.get("attempts")] for job in data.get("ocr_jobs") or []],
            "pending_uploads": len(data.get("pending_uploads") or []),
            "papers": len(data.get("papers") or []),
            "results": len(data.get("results") or []),
            "confirmation_questions": len(data.get("confirmation_questions") or []),
        }])
        return save_meta(self, snapshot=snapshot)

    class RecordingLock:
        def __init__(self, batch_id, upload_dir):
            self.scope = "template" if os.path.basename(upload_dir) == ".template-locks" else "batch"
            self.inner = real_lock(batch_id, upload_dir)

        def __enter__(self):
            try:
                result = self.inner.__enter__()
            except BatchOperationBusy:
                _LEDGER.append(["lock", self.scope, "busy"])
                raise
            _LEDGER.append(["lock", self.scope, "acquire"])
            return result

        def __exit__(self, *exc_info):
            _LEDGER.append(["lock", self.scope, "release"])
            return self.inner.__exit__(*exc_info)

    monkeypatch.setattr(QuickBatch, "save_meta", recording_save_meta)
    monkeypatch.setattr(grade, "batch_operation_lock", RecordingLock)


class _Normalizer:
    """Replace run-specific values with stable, first-seen-ordered labels."""

    _TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
    _HEX32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
    _HEX12 = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")
    _VOLATILE_KEYS = {"created_at", "updated_at", "graded_at", "grading_started_at",
                      "review_ready_at", "grading_elapsed_seconds", "elapsed_seconds"}

    def __init__(self, tmp_path):
        self.roots = sorted({str(tmp_path), os.path.realpath(tmp_path)}, key=len, reverse=True)
        self.labels: dict[str, str] = {}

    def _label(self, kind: str, match: re.Match) -> str:
        key = f"{kind}:{match.group(0)}"
        if key not in self.labels:
            count = sum(1 for existing in self.labels if existing.startswith(f"{kind}:"))
            self.labels[key] = f"<{kind}#{count + 1}>"
        return self.labels[key]

    def text(self, value: str) -> str:
        for root in self.roots:
            if root in value:
                value = value.replace(root, "<TMP>").replace("\\", "/")
        value = self._TIMESTAMP.sub("<TS>", value)
        value = self._HEX32.sub(lambda m: self._label("ID32", m), value)
        return self._HEX12.sub(lambda m: self._label("ID12", m), value)

    def __call__(self, value, key: str = ""):
        if key in self._VOLATILE_KEYS and value not in (None, "", 0):
            return f"<{key}>"
        if isinstance(value, dict):
            return {self.text(str(k)): self(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self(item, key) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, float) and key.endswith("_s"):
            return "<seconds>"
        return value


def _assert_golden(name: str, observed: dict) -> None:
    # Key order is kept: it is part of the bytes an SSE client receives.
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(observed, ensure_ascii=False, indent=1) + "\n"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"missing golden {path}; generate it with MATHAGENT_UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    assert json.loads(rendered) == json.loads(expected)
    assert rendered == expected, "same values, different key order"


def _page_of(image_path: str) -> str:
    """Saved uploads are renamed; every synthetic image ends with its page index."""
    with open(image_path, "rb") as handle:
        index = re.search(rb"(\d+)$", handle.read())
    return f"page-{int(index.group(1))}" if index else os.path.basename(image_path)


class _Boundary:
    """Scripted OCR/identity providers that record the context of every call."""

    def __init__(self, monkeypatch, *, answers=None, identities=None):
        import routers.grade as grade

        self.answers = dict(answers or {})
        self.identities = dict(identities or {})
        self.ocr_calls: list[dict] = []
        self.identity_calls: list[dict] = []

        async def recognize(image_path, batch_id, _paper_cache, *, teacher_id, paper_id):
            name = _page_of(image_path)
            self.ocr_calls.append({
                "page": name,
                "file": os.path.basename(image_path),
                "batch_id": batch_id,
                "teacher_id": teacher_id,
                "paper_id": paper_id,
                "vision_phase_key": ocr_service._vision_phase_key.get(),
                "vision_retry_phase_key": ocr_service._vision_retry_phase_key.get(),
                "vision_batch_paper_count": ocr_service._vision_batch_paper_count.get(),
                "vision_route_ordinal": ocr_service._vision_route_ordinal.get(),
                "grading_teacher_id": analyzer._grading_teacher_id.get(),
            })
            outcome = self.answers.get(name, _answer("2"))
            if isinstance(outcome, BaseException):
                raise outcome
            return copy.deepcopy(outcome)

        async def identity(image_path, roster, *, teacher_id):
            name = _page_of(image_path)
            self.identity_calls.append({"file": name, "roster": list(roster), "teacher_id": teacher_id})
            return copy.deepcopy(self.identities.get(name, {
                "name": "", "student_no": "", "confidence": 0.0,
                "candidate_names": [], "match_status": "no_match",
            }))

        self.recognize = recognize
        monkeypatch.setattr(grade, "recognize_paper_with_cache", recognize)
        monkeypatch.setattr(grade, "recognize_student_info_with_roster", identity)

    def record(self) -> dict:
        return {"ocr_calls": self.ocr_calls, "identity_calls": self.identity_calls}


def _answer(student_answer: str) -> dict:
    return {"student_name": "", "student_no": "", "questions": [{
        "question_number": "1", "question_text": "1+1=?",
        "question_type": "fill", "student_answer": student_answer,
    }]}


def _identity(name: str, student_no: str) -> dict:
    return {"name": name, "student_no": student_no, "confidence": 0.98,
            "candidate_names": [name], "match_status": "exact", "name_written": True}


_RETRYABLE_FAILURE = {
    "questions": [], "student_name": "", "student_no": "", "error": "视觉服务超时",
    "_ocr_retryable": True, "_ocr_error_code": "provider_timeout",
}
_FINAL_FAILURE = {
    "questions": [], "student_name": "", "student_no": "", "error": "图片无法识别",
    "_ocr_retryable": False, "_ocr_error_code": "unreadable",
}
_ACCELERATOR_PENDING = {
    "questions": [], "student_name": "", "student_no": "", "error": "加速节点正在处理",
    "_accelerator_state": "pending", "_accelerator_reason_code": "accelerator_busy",
}


def _seed_students(Session):
    from models import Student

    with Session() as db:
        db.add_all([
            Student(id=1, teacher_id=1, class_id=1, name="张三", student_no="01"),
            Student(id=2, teacher_id=1, class_id=1, name="李四", student_no="02"),
        ])
        db.commit()


def _batch_dirs(tmp_path) -> list[Path]:
    return sorted(tmp_path.glob("quick_batch_*"))


def _lock_released(batch_id: str, tmp_path) -> bool:
    try:
        with batch_operation_lock(batch_id, str(tmp_path)):
            return True
    except BatchOperationBusy:
        return False


def _raw_meta(batch_id: str, tmp_path):
    path = tmp_path / f"quick_batch_{batch_id}" / "meta.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _observe(tmp_path, response, boundary: _Boundary | None, *, batch_id: str | None = None) -> dict:
    events = _events(response) if response.status_code == 200 else []
    if batch_id is None:
        dirs = _batch_dirs(tmp_path)
        batch_id = dirs[0].name.removeprefix("quick_batch_") if len(dirs) == 1 else None
    observed = {
        "status_code": response.status_code,
        "body": None if response.status_code == 200 else response.json(),
        "events": [[event, payload] for event, payload in events],
        "meta": _raw_meta(batch_id, tmp_path) if batch_id else None,
        "batch_dirs": [path.name for path in _batch_dirs(tmp_path)],
        "lock_released": _lock_released(batch_id, tmp_path) if batch_id else None,
        "ledger": list(_LEDGER),
        **(boundary.record() if boundary else {}),
    }
    return _Normalizer(tmp_path)(observed)


def _upload_files(count: int):
    return [
        ("files", (f"paper-{index}.ocr-ready-v1.jpg", f"synthetic-{index}".encode(), "image/jpeg"))
        for index in range(count)
    ]


def _recognize(client, count: int, **form):
    data = {"template_id": TEMPLATE_ID, "mode": "auto", "category": "daily", **form}
    return client.post("/api/batch-grade/recognize", files=_upload_files(count), data=data)


def _template(tmp_path, *, category="daily", exam_name="", pages=1):
    """A confirmed template; fields are set before the contract is bound."""
    from services.template_session import TemplateSession

    session = TemplateSession(TEMPLATE_ID, str(tmp_path), teacher_id=1)
    session.status = "confirmed"
    session.category = category
    session.exam_name = exam_name
    for page in range(pages):
        session.add_page(page, [{
            "question_number": str(page + 1), "question_text": f"{page + 1}+1=?",
            "question_type": "fill", "standard_answer": str(page + 2),
        }])
    session.confirm_current_contract(1)
    session.save_meta()
    return session


def _recognition_client(monkeypatch, tmp_path):
    """Initial recognition requires a teaching stage on the teacher profile."""
    return _setup(monkeypatch, tmp_path, education_stage="senior")


class _FailingQuerySession:
    """A DB session whose first query fails, as an unavailable database would."""

    def __init__(self, inner):
        self._inner = inner

    def query(self, *_args, **_kwargs):
        raise RuntimeError("synthetic database outage")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _break_stream_database(monkeypatch, Session):
    import routers.grade as grade

    monkeypatch.setattr(grade, "SessionLocal", lambda: _FailingQuerySession(Session()))


def _busy_lock(monkeypatch):
    import routers.grade as grade

    real_lock = grade.batch_operation_lock

    def busy(batch_id, upload_dir):
        if os.path.basename(upload_dir) == ".template-locks":
            return real_lock(batch_id, upload_dir)
        _LEDGER.append(["lock", "batch", "busy"])
        raise BatchOperationBusy("synthetic concurrent recognition")

    monkeypatch.setattr(grade, "batch_operation_lock", busy)


# ── initial recognition ─────────────────────────────────────────────────────


def test_recognize_identified_all_pages_succeed(monkeypatch, tmp_path):
    client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    _seed_students(Session)
    boundary = _Boundary(monkeypatch, identities={
        "page-0": _identity("张三", "01"),
        "page-1": _identity("李四", "02"),
    }, answers={"page-1": _answer("3")})

    response = _recognize(client, 3)

    _assert_golden("recognize_identified_all_pages_succeed", _observe(tmp_path, response, boundary))


def test_recognize_identity_inheritance_in_path_order(monkeypatch, tmp_path):
    client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    _seed_students(Session)
    # Upload order differs from natural path order: a/1 (page-1), b/1 (page-2),
    # b/2 (page-0).  page-1 carries the only identity.
    blank = {"name": "", "student_no": "", "confidence": 0.0, "candidate_names": [],
             "match_status": "no_match", "name_written": False}
    # page-2 (another page type) inherits; page-0 repeats the anchor's page
    # type, so it is blocked as a new student who forgot to write a name.
    page_types = {"page-1": "p1", "page-2": "p2", "page-0": "p1"}
    boundary = _Boundary(monkeypatch, identities={
        "page-1": _identity("张三", "01"), "page-2": blank, "page-0": blank,
    }, answers={page: {**_answer("2"), "_cache_type_label": label} for page, label in page_types.items()})
    paths = ["收集/b/2.jpg", "收集/a/1.jpg", "收集/b/1.jpg"]

    response = _recognize(client, 3, inherit_identity="true", source_paths=json.dumps(paths))

    _assert_golden("recognize_identity_inheritance_in_path_order", _observe(tmp_path, response, boundary))


def test_recognize_follower_finishes_before_its_anchor(monkeypatch, tmp_path):
    """A follower ingested first is graded once its anchor arrives (dispatch_inherited)."""
    client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    _seed_students(Session)
    blank = {"name": "", "student_no": "", "confidence": 0.0, "candidate_names": [],
             "match_status": "no_match", "name_written": False}
    boundary = _Boundary(monkeypatch, identities={"page-0": _identity("张三", "01"), "page-1": blank},
                         answers={"page-0": {**_answer("2"), "_cache_type_label": "p1"},
                                  "page-1": {**_answer("2"), "_cache_type_label": "p2"}})
    scripted = boundary.recognize

    def follower_checkpointed() -> bool:
        return any(kind == "checkpoint" and entry["jobs"][1][0] == "succeeded" for kind, *rest in _LEDGER
                   for entry in rest[:1] if isinstance(entry, dict))

    async def anchor_last(image_path, *args, **kwargs):
        result = await scripted(image_path, *args, **kwargs)
        if _page_of(image_path) == "page-0":
            # Event-loop turns only, no wall clock: wait until the follower's
            # OCR outcome has been ingested and checkpointed.
            for _turn in range(10_000):
                if follower_checkpointed():
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("follower page was never checkpointed")
        return result

    import routers.grade as grade
    monkeypatch.setattr(grade, "recognize_paper_with_cache", anchor_last)

    response = _recognize(client, 2, inherit_identity="true")

    _assert_golden("recognize_follower_finishes_before_its_anchor", _observe(tmp_path, response, boundary))


def test_recognize_inspects_uploads_without_orientation(monkeypatch, tmp_path):
    """Uploads saved without diagnostics are hashed and inspected inside the stream."""
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    import routers.grade as grade

    inspected = grade._client_ocr_ready_orientation
    saving = {"active": True}
    real_save = grade._save_batch_uploads

    async def save_without_diagnostics(*args, **kwargs):
        saved = await real_save(*args, **kwargs)
        saving["active"] = False
        return [(name, path, "") for name, path, _sha in saved]

    monkeypatch.setattr(grade, "_save_batch_uploads", save_without_diagnostics)
    monkeypatch.setattr(grade, "_client_ocr_ready_orientation",
                        lambda filename: {} if saving["active"] else inspected(filename))

    response = _recognize(client, 3, anonymous="true")

    _assert_golden("recognize_inspects_uploads_without_orientation", _observe(tmp_path, response, boundary))


def test_recognize_anonymous_mixed_outcomes(monkeypatch, tmp_path):
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch, answers={
        "page-1": RuntimeError("provider exploded"),
        "page-2": _FINAL_FAILURE,
        "page-3": _RETRYABLE_FAILURE,
    })

    response = _recognize(client, 4, anonymous="true")

    _assert_golden("recognize_anonymous_mixed_outcomes", _observe(tmp_path, response, boundary))


def test_recognize_exam_precise_mode(monkeypatch, tmp_path):
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path, category="exam", exam_name="期中")
    boundary = _Boundary(monkeypatch)

    response = _recognize(client, 1, anonymous="true", category="exam", exam_name="期中", mode="precise")

    _assert_golden("recognize_exam_precise_mode", _observe(tmp_path, response, boundary))


def test_recognize_accelerator_wait_pauses_batch(monkeypatch, tmp_path):
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch, answers={"page-1": _ACCELERATOR_PENDING})

    response = _recognize(client, 2, anonymous="true")

    _assert_golden("recognize_accelerator_wait_pauses_batch", _observe(tmp_path, response, boundary))


def test_recognize_every_page_retryable_failure(monkeypatch, tmp_path):
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch, answers={
        f"page-{index}": _RETRYABLE_FAILURE for index in range(2)
    })

    response = _recognize(client, 2, anonymous="true")

    _assert_golden("recognize_every_page_retryable_failure", _observe(tmp_path, response, boundary))


def test_recognize_busy_batch_lock(monkeypatch, tmp_path):
    client, _Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    _busy_lock(monkeypatch)

    response = _recognize(client, 1, anonymous="true")

    _assert_golden("recognize_busy_batch_lock", _observe(tmp_path, response, boundary))


def test_recognize_stream_database_failure(monkeypatch, tmp_path):
    client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    _break_stream_database(monkeypatch, Session)

    response = _recognize(client, 2)

    _assert_golden("recognize_stream_database_failure", _observe(tmp_path, response, boundary))


def test_recognize_disconnect_after_first_event(monkeypatch, tmp_path):
    _client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    import routers.grade as grade

    async def scenario():
        db = Session()
        try:
            response = await grade.batch_grade_recognize(
                files=[UploadFile(filename=f"paper-{index}.ocr-ready-v1.jpg", file=BytesIO(f"synthetic-{index}".encode()))
                       for index in range(2)],
                category="daily", exam_name="", template_id=TEMPLATE_ID, mode="auto",
                anonymous=True, source_paths="", inherit_identity=False,
                user=User(id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior"),
                db=db,
            )
            iterator = response.body_iterator
            first = await anext(iterator)
            await iterator.aclose()
            return first.decode("utf-8") if isinstance(first, bytes) else first
        finally:
            db.close()

    first_event = asyncio.run(scenario())
    batch_id = _batch_dirs(tmp_path)[0].name.removeprefix("quick_batch_")
    observed = _Normalizer(tmp_path)({
        "first_event": first_event,
        "meta": _raw_meta(batch_id, tmp_path),
        "lock_released": _lock_released(batch_id, tmp_path),
        "ledger": list(_LEDGER),
        **boundary.record(),
    })
    _assert_golden("recognize_disconnect_after_first_event", observed)


class _Limiter:
    def __init__(self, *, daily_allowed=True, reserve_allowed=True):
        self.daily_allowed = daily_allowed
        self.reserve_allowed = reserve_allowed
        self.calls: list[list] = []

    def check_daily_image_limit(self, user_id, requested):
        self.calls.append(["check_daily_image_limit", user_id, requested])
        return {"allowed": self.daily_allowed, "current": 9, "limit": 10,
                "requested": requested, "remaining": 1, "reset_time": "tomorrow"}

    def reserve_student_images(self, user_id, total, *, batch_id):
        self.calls.append(["reserve_student_images", user_id, total, batch_id])
        return {"allowed": self.reserve_allowed, "current": 9, "limit": 10, "requested": total}


@pytest.mark.parametrize("case", [
    "bad_category", "exam_without_name", "bad_mode", "no_files", "bad_source_paths",
    "no_class", "no_template", "unsaved_template", "template_category_mismatch",
    "foreign_template", "daily_limit", "reservation_refused", "reservation_allowed",
])
def test_recognize_preflight(monkeypatch, tmp_path, case):
    client, Session = _recognition_client(monkeypatch, tmp_path)
    import routers.grade as grade
    from routers.auth import _require_user

    template = _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    limiter = _Limiter(
        daily_allowed=case != "daily_limit",
        reserve_allowed=case != "reservation_refused",
    )
    monkeypatch.setattr(grade, "_check_limits_or_raise", lambda _db, _user_id: limiter)
    form = {"anonymous": "true"}
    files = _upload_files(2)
    if case == "bad_category":
        form["category"] = "weekly"
    elif case == "exam_without_name":
        form["category"] = "exam"
    elif case == "bad_mode":
        form["mode"] = "fast"
    elif case == "no_files":
        files = []
    elif case == "bad_source_paths":
        form["source_paths"] = json.dumps(["../escape.jpg", "b.jpg"])
    elif case == "no_class":
        form.pop("anonymous")
        client.app.dependency_overrides[_require_user] = lambda: User(
            id=1, name="老师", role="teacher", current_class_id=None, education_stage="senior",
        )
    elif case == "no_template":
        form["template_id"] = "  "
    elif case == "unsaved_template":
        template.status = "pending"
        template.save_meta()
    elif case == "template_category_mismatch":
        template = _template(tmp_path, category="exam", exam_name="期中")
    elif case == "foreign_template":
        template.teacher_id = 2
        template.save_meta()

    data = {"template_id": TEMPLATE_ID, "mode": "auto", "category": "daily", **form}
    response = client.post("/api/batch-grade/recognize", files=files or None, data=data)

    observed = _observe(tmp_path, response, boundary)
    observed["limiter_calls"] = _Normalizer(tmp_path)(limiter.calls)
    template_meta = tmp_path / f"template_{TEMPLATE_ID}" / "meta.json"
    if template_meta.exists():
        saved = json.loads(template_meta.read_text(encoding="utf-8"))
        observed["template_status"] = saved.get("status")
    _assert_golden(f"recognize_preflight_{case}", observed)


# ── recognition resume ──────────────────────────────────────────────────────


def _checkpoint(tmp_path, *, batch_id="resume-characterization", status="recognition_interrupted",
                jobs=("succeeded", "succeeded", "queued"), attempts=None, identity_mode="anonymous",
                with_papers=None, two_page_template=False):
    """Persist a template batch with one upload per entry of ``jobs``."""
    batch = QuickBatch(batch_id, str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.identity_mode = identity_mode
    batch.grade_mode = "auto"
    batch.category = "daily"
    batch.source_mode = "template"
    batch.status = status
    batch.error = "识别连接中断，等待恢复"
    batch.template_id = _template(tmp_path, pages=2 if two_page_template else 1).template_id
    directory = tmp_path / f"quick_batch_{batch.batch_id}"
    directory.mkdir()
    attempts = list(attempts or [1 if state != "queued" else 0 for state in jobs])
    with_papers = list(with_papers if with_papers is not None else [state == "succeeded" for state in jobs])
    for index, _state in enumerate(jobs):
        image = directory / f"paper-{index}.png"
        image.write_bytes(f"synthetic-image-{index}".encode())
        upload = {
            "paper_id": f"paper-{index}", "filename": image.name,
            "image_path": str(image), "source_sha256": f"source-{index}",
            "working_sha256": f"working-{index}",
            "orientation_status": "auto_passed", "orientation_reason": "normal_angle",
            "orientation_confidence": 1.0, "detected_tilt_degrees": 0.0,
        }
        batch.uploads.append(upload)
        if with_papers[index]:
            batch.papers.append({
                **upload, "student_name": "", "student_no": "",
                "identity": {"status": "anonymous"},
                "questions": [{"question_no": "1", "question_type": "fill", "student_answer": "2"}],
            })
    batch.questions = [{"question_no": "1", "question_text": "1+1=?", "question_type": "fill"}]
    batch.ensure_ocr_jobs(batch.uploads)
    for job, state, spent in zip(batch.ocr_jobs, jobs, attempts):
        job.update(status=state, attempts=spent)
        if state == "failed":
            job.update(last_error="视觉服务超时", retryable=True, error_code="provider_timeout")
    batch.save_meta()
    return batch


def _resume(client, batch_id="resume-characterization"):
    return client.post(f"/api/batch-grade/{batch_id}/recognize/resume")


def test_resume_runs_queued_page_and_finalizes(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path)
    boundary = _Boundary(monkeypatch, answers={"page-2": _answer("4")})

    response = _resume(client)

    _assert_golden("resume_runs_queued_page_and_finalizes",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_inspects_legacy_uploads(monkeypatch, tmp_path):
    """A checkpoint from before upload hashing is inspected before OCR resumes."""
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = _checkpoint(tmp_path, jobs=("succeeded", "queued", "queued"))
    for upload in batch.uploads[1:]:
        for key in ("source_sha256", "working_sha256", "orientation_status", "orientation_reason",
                    "orientation_confidence", "detected_tilt_degrees"):
            upload.pop(key)
    batch.save_meta()
    boundary = _Boundary(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_inspects_legacy_uploads",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_identified_batch_reads_roster(monkeypatch, tmp_path):
    client, Session = _setup(monkeypatch, tmp_path)
    _seed_students(Session)
    _checkpoint(tmp_path, identity_mode="identified", jobs=("queued", "queued"))
    boundary = _Boundary(monkeypatch, identities={
        "page-0": _identity("张三", "01"),
        "page-1": _identity("李四", "02"),
    })

    response = _resume(client)

    _assert_golden("resume_identified_batch_reads_roster",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_requeues_retryable_failure(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, status="ocr_partial_failed", jobs=("succeeded", "failed"))
    boundary = _Boundary(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_requeues_retryable_failure",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_terminal_checkpoint_only_finalizes(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, status="recognizing_remaining", jobs=("succeeded", "succeeded"))
    boundary = _Boundary(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_terminal_checkpoint_only_finalizes",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_without_retryable_work(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, jobs=("succeeded", "succeeded"), with_papers=[True, False])
    boundary = _Boundary(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_without_retryable_work",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_exhausted_attempts(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, status="failed", jobs=("failed", "queued"), attempts=[2, 2])
    boundary = _Boundary(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_exhausted_attempts",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_accelerator_wait(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path)
    boundary = _Boundary(monkeypatch, answers={"page-2": _ACCELERATOR_PENDING})

    response = _resume(client)

    _assert_golden("resume_accelerator_wait",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


@pytest.mark.parametrize("second_attempt", [False, True])
def test_resume_every_queued_page_fails(monkeypatch, tmp_path, second_attempt):
    client, _Session = _setup(monkeypatch, tmp_path)
    spent = 1 if second_attempt else 0
    _checkpoint(tmp_path, jobs=("queued", "queued"), attempts=[spent, spent])
    boundary = _Boundary(monkeypatch, answers={
        "page-0": _RETRYABLE_FAILURE, "page-1": _RETRYABLE_FAILURE,
    })

    response = _resume(client)

    suffix = "second_attempt" if second_attempt else "first_attempt"
    _assert_golden(f"resume_every_queued_page_fails_{suffix}",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_two_page_template_exhausts_last_attempt(monkeypatch, tmp_path):
    """Without single-page placeholders an exhausted page leaves no paper at all."""
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, jobs=("queued",), attempts=[1], two_page_template=True)
    boundary = _Boundary(monkeypatch, answers={"page-0": _RETRYABLE_FAILURE})

    response = _resume(client)

    _assert_golden("resume_two_page_template_exhausts_last_attempt",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_busy_batch_lock(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path)
    boundary = _Boundary(monkeypatch)
    _busy_lock(monkeypatch)

    response = _resume(client)

    _assert_golden("resume_busy_batch_lock",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


@pytest.mark.parametrize("change", ["status", "source_mode", "template_id"])
def test_resume_checkpoint_changed_before_lock(monkeypatch, tmp_path, change):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade

    _checkpoint(tmp_path)
    boundary = _Boundary(monkeypatch)
    real_lock = grade.batch_operation_lock

    def lock_after_concurrent_change(batch_id, upload_dir):
        meta = tmp_path / f"quick_batch_{batch_id}" / "meta.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data[change] = {"status": "ocr_done", "source_mode": "", "template_id": ""}[change]
        meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return real_lock(batch_id, upload_dir)

    monkeypatch.setattr(grade, "batch_operation_lock", lock_after_concurrent_change)

    response = _resume(client)

    _assert_golden(f"resume_checkpoint_changed_before_lock_{change}",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_stream_database_failure(monkeypatch, tmp_path):
    client, Session = _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, identity_mode="identified")
    boundary = _Boundary(monkeypatch)
    _break_stream_database(monkeypatch, Session)

    response = _resume(client)

    _assert_golden("resume_stream_database_failure",
                   _observe(tmp_path, response, boundary, batch_id="resume-characterization"))


def test_resume_disconnect_while_page_is_recognized(monkeypatch, tmp_path):
    _client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade

    _checkpoint(tmp_path, jobs=("succeeded", "queued"))
    boundary = _Boundary(monkeypatch)
    started = asyncio.Event()
    recognize = grade.recognize_paper_with_cache

    async def blocked(image_path, batch_id, paper_cache, **kwargs):
        await recognize(image_path, batch_id, paper_cache, **kwargs)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(grade, "recognize_paper_with_cache", blocked)

    async def scenario():
        response = await grade.batch_grade_resume_recognition(
            "resume-characterization",
            User(id=1, name="老师", role="teacher", current_class_id=1),
        )
        chunks = []

        async def consume():
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        return chunks

    chunks = asyncio.run(scenario())
    observed = _Normalizer(tmp_path)({
        "chunks": chunks,
        "meta": _raw_meta("resume-characterization", tmp_path),
        "lock_released": _lock_released("resume-characterization", tmp_path),
        "ledger": list(_LEDGER),
        **boundary.record(),
    })
    _assert_golden("resume_disconnect_while_page_is_recognized", observed)


# ── clients that stop reading mid-stream ────────────────────────────────────
#
# A client that goes away leaves the generator suspended at a ``yield``; the
# stream is then closed (GeneratorExit), not cancelled.  Where it was
# suspended decides whether the batch is reopened as interrupted.


def _decode(chunk) -> list:
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
    lines = text.splitlines()
    event = next(line[7:] for line in lines if line.startswith("event: "))
    return [event, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))]


async def _read_until_then_close(response, stop) -> list[list]:
    """Read events until ``stop(event, payload)`` holds, then close like a client."""
    iterator = response.body_iterator
    seen = []
    async for chunk in iterator:
        seen.append(_decode(chunk))
        if stop(*seen[-1]):
            break
    await iterator.aclose()
    return seen


_STOPS = {
    "first_inspection": lambda event, payload: event == "progress" and payload["message"].startswith(("已完成", "恢复时已检查")),
    "first_ocr_outcome": lambda event, payload: event == "progress" and payload["message"].startswith("已识别"),
    "first_result": lambda event, payload: event == "result",
    "first_paper": lambda event, payload: event == "paper",
    "error": lambda event, payload: event == "error",
    "done": lambda event, payload: event == "done",
}


def _closed_stream_observation(tmp_path, batch_id: str, seen: list, boundary: _Boundary) -> dict:
    return _Normalizer(tmp_path)({
        "events": seen,
        "meta": _raw_meta(batch_id, tmp_path),
        "lock_released": _lock_released(batch_id, tmp_path),
        "ledger": list(_LEDGER),
        **boundary.record(),
    })


def _direct_recognize(Session, count: int):
    import routers.grade as grade

    async def call():
        db = Session()
        try:
            return await grade.batch_grade_recognize(
                files=[UploadFile(filename=f"paper-{index}.ocr-ready-v1.jpg",
                                  file=BytesIO(f"synthetic-{index}".encode()))
                       for index in range(count)],
                category="daily", exam_name="", template_id=TEMPLATE_ID, mode="auto",
                anonymous=True, source_paths="", inherit_identity=False,
                user=User(id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior"),
                db=db,
            )
        finally:
            db.close()

    return call()


@pytest.mark.parametrize("stop_at", ["first_ocr_outcome", "first_result", "first_paper", "done"])
def test_recognize_client_closes_stream(monkeypatch, tmp_path, stop_at):
    _client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)

    async def scenario():
        return await _read_until_then_close(await _direct_recognize(Session, 3), _STOPS[stop_at])

    seen = asyncio.run(scenario())
    assert _STOPS[stop_at](*seen[-1]), seen
    batch_id = _batch_dirs(tmp_path)[0].name.removeprefix("quick_batch_")
    _assert_golden(f"recognize_client_closes_at_{stop_at}",
                   _closed_stream_observation(tmp_path, batch_id, seen, boundary))


def test_recognize_client_closes_during_inspection(monkeypatch, tmp_path):
    _client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    import routers.grade as grade

    inspected = grade._client_ocr_ready_orientation
    saving = {"active": True}
    real_save = grade._save_batch_uploads

    async def save_without_diagnostics(*args, **kwargs):
        saved = await real_save(*args, **kwargs)
        saving["active"] = False
        return [(name, path, "") for name, path, _sha in saved]

    monkeypatch.setattr(grade, "_save_batch_uploads", save_without_diagnostics)
    monkeypatch.setattr(grade, "_client_ocr_ready_orientation",
                        lambda filename: {} if saving["active"] else inspected(filename))

    async def scenario():
        return await _read_until_then_close(await _direct_recognize(Session, 3), _STOPS["first_inspection"])

    seen = asyncio.run(scenario())
    assert _STOPS["first_inspection"](*seen[-1]), seen
    batch_id = _batch_dirs(tmp_path)[0].name.removeprefix("quick_batch_")
    _assert_golden("recognize_client_closes_during_inspection",
                   _closed_stream_observation(tmp_path, batch_id, seen, boundary))


def test_recognize_cancelled_while_page_is_recognized(monkeypatch, tmp_path):
    _client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch)
    import routers.grade as grade

    started = asyncio.Event()
    scripted = boundary.recognize

    async def second_page_blocks(image_path, *args, **kwargs):
        result = await scripted(image_path, *args, **kwargs)
        if _page_of(image_path) == "page-1":
            started.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(grade, "recognize_paper_with_cache", second_page_blocks)

    async def scenario():
        response = await _direct_recognize(Session, 2)
        chunks = []

        async def consume():
            async for chunk in response.body_iterator:
                chunks.append(_decode(chunk))

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        return chunks

    seen = asyncio.run(scenario())
    batch_id = _batch_dirs(tmp_path)[0].name.removeprefix("quick_batch_")
    _assert_golden("recognize_cancelled_while_page_is_recognized",
                   _closed_stream_observation(tmp_path, batch_id, seen, boundary))


def _direct_resume():
    import routers.grade as grade

    return grade.batch_grade_resume_recognition(
        "resume-characterization",
        User(id=1, name="老师", role="teacher", current_class_id=1),
    )


@pytest.mark.parametrize("stop_at", ["first_inspection", "first_ocr_outcome", "first_paper", "done"])
def test_resume_client_closes_stream(monkeypatch, tmp_path, stop_at):
    _setup(monkeypatch, tmp_path)
    batch = _checkpoint(tmp_path, jobs=("succeeded", "queued", "queued"))
    if stop_at == "first_inspection":
        for upload in batch.uploads[1:]:
            for key in ("source_sha256", "working_sha256", "orientation_status", "orientation_reason",
                        "orientation_confidence", "detected_tilt_degrees"):
                upload.pop(key)
        batch.save_meta()
    boundary = _Boundary(monkeypatch)

    async def scenario():
        return await _read_until_then_close(await _direct_resume(), _STOPS[stop_at])

    seen = asyncio.run(scenario())
    assert _STOPS[stop_at](*seen[-1]), seen
    _assert_golden(f"resume_client_closes_at_{stop_at}",
                   _closed_stream_observation(tmp_path, "resume-characterization", seen, boundary))


def test_client_closes_at_partial_done(monkeypatch, tmp_path):
    """A done event over a partially failed ledger is final once it is sent."""
    _client, Session = _recognition_client(monkeypatch, tmp_path)
    _template(tmp_path)
    boundary = _Boundary(monkeypatch, answers={"page-1": _RETRYABLE_FAILURE})

    async def scenario():
        return await _read_until_then_close(await _direct_recognize(Session, 2), _STOPS["done"])

    seen = asyncio.run(scenario())
    assert seen[-1][0] == "done" and seen[-1][1]["status"] == "ocr_partial_failed", seen
    batch_id = _batch_dirs(tmp_path)[0].name.removeprefix("quick_batch_")
    _assert_golden("recognize_client_closes_at_partial_done",
                   _closed_stream_observation(tmp_path, batch_id, seen, boundary))


def test_resume_client_closes_at_partial_done(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _checkpoint(tmp_path, jobs=("succeeded", "queued"))
    boundary = _Boundary(monkeypatch, answers={"page-1": _RETRYABLE_FAILURE})

    async def scenario():
        return await _read_until_then_close(await _direct_resume(), _STOPS["done"])

    seen = asyncio.run(scenario())
    assert seen[-1][0] == "done" and seen[-1][1]["status"] == "ocr_partial_failed", seen
    _assert_golden("resume_client_closes_at_partial_done",
                   _closed_stream_observation(tmp_path, "resume-characterization", seen, boundary))


@pytest.mark.parametrize("outcome", ["no_retryable_work", "accelerator_wait", "every_page_fails"])
def test_resume_client_closes_on_final_error(monkeypatch, tmp_path, outcome):
    """The final error event is sent before the outcome counts as delivered."""
    _setup(monkeypatch, tmp_path)
    if outcome == "no_retryable_work":
        _checkpoint(tmp_path, jobs=("succeeded", "succeeded"), with_papers=[True, False])
        boundary = _Boundary(monkeypatch)
    elif outcome == "accelerator_wait":
        _checkpoint(tmp_path)
        boundary = _Boundary(monkeypatch, answers={"page-2": _ACCELERATOR_PENDING})
    else:
        _checkpoint(tmp_path, jobs=("queued", "queued"))
        boundary = _Boundary(monkeypatch, answers={
            "page-0": _RETRYABLE_FAILURE, "page-1": _RETRYABLE_FAILURE,
        })

    async def scenario():
        return await _read_until_then_close(await _direct_resume(), _STOPS["error"])

    seen = asyncio.run(scenario())
    assert seen[-1][0] == "error", seen
    _assert_golden(f"resume_client_closes_on_final_error_{outcome}",
                   _closed_stream_observation(tmp_path, "resume-characterization", seen, boundary))


@pytest.mark.parametrize("case", ["missing", "foreign", "not_template", "no_template", "not_resumable"])
def test_resume_preflight(monkeypatch, tmp_path, case):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = _checkpoint(tmp_path)
    boundary = _Boundary(monkeypatch)
    batch_id = batch.batch_id
    if case == "missing":
        batch_id = "no-such-batch"
    elif case == "foreign":
        batch.teacher_id = 2
    elif case == "not_template":
        batch.source_mode = "legacy"
    elif case == "no_template":
        batch.template_id = ""
    elif case == "not_resumable":
        batch.status = "grading"
    batch.save_meta()

    response = _resume(client, batch_id)

    _assert_golden(f"resume_preflight_{case}",
                   _observe(tmp_path, response, boundary, batch_id=batch.batch_id))


def test_direct_resume_call_rejects_before_streaming(tmp_path, monkeypatch):
    """The preflight raises HTTPException before a StreamingResponse exists."""
    _setup(monkeypatch, tmp_path)
    import routers.grade as grade

    batch = _checkpoint(tmp_path, status="ocr_done")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(grade.batch_grade_resume_recognition(
            batch.batch_id, User(id=1, name="老师", role="teacher", current_class_id=1),
        ))
    assert (raised.value.status_code, raised.value.detail) == (409, "该批次当前不需要恢复识别")
