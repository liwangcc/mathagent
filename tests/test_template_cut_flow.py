from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw
from pydantic import ValidationError

from routers import grade
from routers import grade_template
from services.paper_orientation import sha256_file
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_evidence import build_template_artifact
from services.template_session import TemplateSession
from tests.grade_family import set_grade_global
from routers import grade_recognition


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Limiter:
    def __init__(self):
        self.events = []

    def record_usage(self, teacher_id, count, *, batch_id, source):
        self.events.append((teacher_id, count, batch_id, source))


def _template_session(tmp_path: Path, template_id: str = "templatecut1"):
    session = TemplateSession(template_id, str(tmp_path), teacher_id=7)
    Path(session.template_dir).mkdir(parents=True)
    source = Path(session.template_dir) / "source.png"
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((50, 80), "1. 1 + 1 = ____", fill="black")
    draw.line((474, 96, 520, 142), fill="black", width=5)
    image.save(source)
    questions = [{
        "question_number": "1",
        "question_text": "1+1=？",
        "question_type": "fill",
        "standard_answer": "2",
        "answer_bbox": [500, 50, 900, 200],
    }]
    artifact = build_template_artifact(
        source,
        session.template_dir,
        0,
        questions,
        blank_detection_enabled=True,
        question_anchors=[{
            "question_number": "1",
            "question_number_bbox": [50, 60, 100, 100],
        }],
        template_handwriting_regions=[{
            "question_number": "1",
            "regions": [[520, 70, 580, 130]],
        }],
    )
    session.add_page(0, questions, artifact)
    session.status = "ocr_done"
    session.save_meta()
    return session, artifact


def _two_page_session(tmp_path: Path, template_id: str = "templateedit1"):
    """Two pages numbered 1..2 and 3..4, the shape a shift has to cross."""
    session = TemplateSession(template_id, str(tmp_path), teacher_id=7)
    Path(session.template_dir).mkdir(parents=True)
    numbers_by_page = [["1", "2"], ["3", "4"]]
    for page_index, numbers in enumerate(numbers_by_page):
        source = Path(session.template_dir) / f"source-{page_index}.png"
        Image.new("RGB", (900, 1200), "white").save(source)
        questions = [
            {
                "question_number": number,
                "question_text": f"题{number}",
                "question_type": "fill",
                "standard_answer": f"答{number}",
            }
            for number in numbers
        ]
        artifact = build_template_artifact(
            source,
            session.template_dir,
            page_index,
            questions,
            blank_detection_enabled=True,
            question_anchors=[
                {
                    "question_number": number,
                    "question_number_bbox": [50, 60 + offset * 100, 100, 100 + offset * 100],
                }
                for offset, number in enumerate(numbers)
            ],
            template_handwriting_regions=[
                {
                    "question_number": number,
                    "regions": [[520, 70 + offset * 100, 580, 130 + offset * 100]],
                }
                for offset, number in enumerate(numbers)
            ],
        )
        # The crop plan carries its own copy of the question sequence, so a
        # renumbering has to reach it too.  Building it here keeps that fifth
        # collection under test rather than absent.
        artifact["question_crop_plan"] = build_anchor_crop_plan(
            {
                "column_layout": {
                    "column_count": 1,
                    "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
                }
            },
            artifact_sha256=artifact["sha256"],
            expected_question_numbers=numbers,
        )
        session.add_page(page_index, questions, artifact)
    session.status = "ocr_done"
    session.save_meta()
    return session


def _cut_result(artifact, request_id="request-1"):
    return {
        "provider": "xiaomi_mimo",
        "operation": "RecognizeColumnLayout",
        "cut_type": "question",
        "subject": "Math",
        "output_oricoord": True,
        "request_id": request_id,
        "source": {
            "sha256": artifact["sha256"],
            "width": artifact["width"],
            "height": artifact["height"],
            "size_bytes": 100,
        },
        "provider_dimensions": {
            "org_width": artifact["width"],
            "org_height": artifact["height"],
            "rectified_width": artifact["width"],
            "rectified_height": artifact["height"],
        },
        "pages": [{
            "page_index": 0,
            "provider_page_no": 1,
            "provider_dimensions": {},
            "candidates": [{
                "candidate_index": 0,
                "candidate_role": "question_block",
                "question_number_hint": "",
                "question_number_hint_source": "",
                "coordinate_trust": "model_coarse",
                "usable_for_original_overlay": True,
                "template_bbox_1000": [0, 0, 1000, 1000],
            }],
        }],
        "summary": {
            "total": 1,
            "original": 1,
            "rectified_untrusted": 0,
            "invalid": 0,
            "missing": 0,
        },
        "column_layout": {
            "column_count": 1,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 1000},
            ],
        },
    }


def test_template_cut_response_exposes_verified_answer_regions(tmp_path):
    """Blank-template answer boxes must reach the teacher preview payload."""
    session, artifact = _template_session(tmp_path, "templatecut-answer-regions")
    answer_regions = [{
        "question_number": "1",
        "regions": [[470, 80, 650, 150]],
    }]
    session.page_artifacts["page_1"]["template_answer_regions"] = answer_regions
    run = {
        "status": "complete",
        "action_id": "template-cut-answer-regions",
        "cut_types": ["question"],
        "image_type": "template",
        "pages": {
            "page_1": {
                "page_index": 0,
                "modes": {"question": _cut_result(artifact)},
            },
        },
    }

    payload = grade_template._template_cut_response(session, run)

    assert payload["pages"][0]["template_answer_regions"] == answer_regions


@pytest.mark.parametrize(
    ("legacy_page_run", "expected"),
    [
        (
            {"question_anchor_candidates": [{"question_number": "1"}]},
            (True, True),
        ),
        ({"anchor_diagnostics": {"needs_review": True}}, (True, True)),
        ({"template_ocr_diagnostics": {"needs_review": True}}, (False, True)),
    ],
)
def test_legacy_geometry_review_signals_fail_closed(legacy_page_run, expected):
    assert grade_template._template_geometry_review_state(legacy_page_run) == expected


@pytest.mark.anyio
async def test_template_cut_is_owned_persisted_and_idempotent(tmp_path, monkeypatch):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, artifact = _template_session(tmp_path)
    calls = []
    limiter = _Limiter()

    async def fake_question_cut(image_path, *, teacher_id="system", column_mode=None):
        assert teacher_id == "7"
        calls.append((Path(image_path), "question", "mimo"))
        return _cut_result(artifact)

    monkeypatch.setattr(grade_template, "recognize_column_layout", fake_question_cut)
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, teacher_id: limiter)
    request = grade_template.TemplateCutCandidatesReq(
        action_id="template-cut-action-1",
        cut_types=["question"],
    )
    user = SimpleNamespace(id=7)

    first = await grade_template.batch_grade_template_cut_candidates(
        session.template_id, request, user=user, db=object()
    )
    replay = await grade_template.batch_grade_template_cut_candidates(
        session.template_id, request, user=user, db=object()
    )

    assert first["success"] is True
    assert replay == first
    assert first["summary"]["original"] == 1
    assert first["candidate_contract"]["answer"].endswith("禁止直接作为 answer_bbox")
    assert first["candidate_contract"]["anchor_crop_plan"].endswith("一次发送给视觉模型")
    plan = first["pages"][0]["anchor_crop_plan"]
    assert plan["provider_bottom_ignored"] is True
    assert plan["requires_question_number_gate"] is True
    assert plan["expected_question_numbers"] == ["1"]
    assert plan["coverage"]["vertical_gaps"] == 0
    assert [block["column"] for block in plan["blocks"]] == ["col_1"]
    assert len(calls) == 1
    assert calls[0][1] == "question"
    assert calls[0][2] == "mimo"
    assert calls[0][0].name == "template.png"
    assert calls[0][0].parent.parent.name == "artifacts"
    assert limiter.events == []

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert restored.layout_candidates["active_action_id"] == request.action_id
    run = restored.layout_cut_runs[request.action_id]
    assert run["status"] == "complete"
    assert run["pages"]["page_1"]["artifact_sha256"] == artifact["sha256"]
    assert run["pages"]["page_1"]["modes"]["question"]["source"]["sha256"] == artifact["sha256"]
    assert run["pages"]["page_1"]["anchor_crop_plan"] == plan
    assert restored.page_artifacts["page_1"]["question_crop_plan"] == plan

    hydrated = grade_template.batch_grade_template_cut_candidates_get(
        session.template_id, user=user
    )
    assert hydrated == first

    preview = grade_template.batch_grade_template_page_image(
        session.template_id, 0, user=user
    )
    assert isinstance(preview, FileResponse)
    assert Path(preview.path).name == "template.png"
    assert preview.headers["cache-control"] == "private, no-store"

    saved_layout = grade_template.batch_grade_template_layout_overrides(
        session.template_id,
        grade_template.TemplateLayoutOverridesReq(
            page_index=0,
            columns=[{
                "column_index": 1,
                "x1_1000": 0,
                "x2_1000": 1000,
            }],
            question_anchors=[{
                "question_number": "1",
                "question_number_bbox": [55, 65, 110, 110],
            }],
            template_handwriting_regions=[{
                "question_number": "1",
                "regions": [[525, 75, 590, 140]],
            }],
        ),
        user=user,
    )
    # The teacher edit is a server-bound revision: every column carries an
    # immutable identity, revision, pixel endpoints, and the source image hash.
    saved_column = saved_layout["columns"][0]
    assert len(saved_layout["columns"]) == 1
    assert (saved_column["column_index"], saved_column["x1_1000"], saved_column["x2_1000"]) == (1, 0, 1000)
    assert saved_column["column_id"].startswith("col_1-")
    assert saved_column["column_order"] == 1
    assert saved_column["layout_revision"] == saved_layout["layout_revision"] >= 1
    assert saved_column["image_sha256"] == artifact["sha256"]
    assert saved_column["source_width"] == artifact["width"]
    assert saved_column["source_height"] == artifact["height"]
    assert saved_column["crop_box_px"] == [0, 0, artifact["width"], artifact["height"]]
    assert saved_layout["column_mode"] == "single"
    # Same boundaries as the frozen plan: no column-derived cache is invalidated.
    assert saved_layout["layout_changed"] is False
    assert saved_layout["question_anchors"][0]["question_number_bbox"] == [
        55, 65, 110, 110,
    ]
    persisted = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert persisted.page_artifacts["page_1"]["question_anchors"] == (
        saved_layout["question_anchors"]
    )
    assert persisted.page_artifacts["page_1"][
        "template_handwriting_regions"
    ] == saved_layout["template_handwriting_regions"]
    persisted_artifact = persisted.page_artifacts["page_1"]
    assert persisted_artifact["template_image"] == artifact["template_image"]
    assert persisted_artifact["sha256"] == artifact["sha256"]
    difference_path = Path(persisted.template_dir) / persisted_artifact[
        "difference_template_image"
    ]
    assert difference_path.name == "difference-template.png"
    assert difference_path.is_file()
    assert sha256_file(difference_path) == persisted_artifact[
        "difference_template_sha256"
    ]
    with Image.open(Path(persisted.template_dir) / persisted_artifact["template_image"]) as original:
        original_rgb = original.convert("RGB")
        assert original_rgb.getpixel((476, 98)) == (0, 0, 0)
    with Image.open(difference_path) as difference:
        difference_rgb = difference.convert("RGB")
        # The teacher's revised normalized rectangle is applied atomically.
        assert difference_rgb.getpixel((476, 98)) == (255, 255, 255)
    hydrated_after_edit = grade_template.batch_grade_template_cut_candidates_get(
        session.template_id, user=user
    )
    assert hydrated_after_edit["pages"][0]["question_anchors"] == (
        saved_layout["question_anchors"]
    )
    assert hydrated_after_edit["pages"][0][
        "template_handwriting_regions"
    ] == saved_layout["template_handwriting_regions"]
    # Ordinary-paper answer boxes are no longer owned by the template artifact;
    # the question schema remains the persisted identity contract instead.
    assert persisted.page_artifacts["page_1"]["question_schema"] == session.page_artifacts["page_1"]["question_schema"]
    assert "answer_regions" not in persisted.page_artifacts["page_1"]

    with pytest.raises(HTTPException) as missing_page:
        grade_template.batch_grade_template_page_image(session.template_id, 1, user=user)
    assert missing_page.value.status_code == 404

    with pytest.raises(HTTPException) as denied_preview:
        grade_template.batch_grade_template_page_image(
            session.template_id, 0, user=SimpleNamespace(id=8)
        )
    assert denied_preview.value.status_code == 403

    confirmed = grade_template.batch_grade_template_confirm(
        grade_template.TemplateConfirmReq(
            template_id=session.template_id,
            category="daily",
            pages=[{
                "page_index": 0,
                "questions": [{
                    "question_no": "1",
                    "question_text": "1+1=？",
                    "question_type": "fill",
                    "standard_answer": "2",
                }],
            }],
        ),
        user=user,
    )
    assert confirmed["success"] is True
    confirmed_session = TemplateSession.load_meta(session.template_id, str(tmp_path))
    confirmed_plan = confirmed_session.page_artifacts["page_1"]["question_crop_plan"]
    assert confirmed_plan["expected_question_numbers"] == ["1"]
    # Confirmation synchronizes the teacher-revised plan (with server-bound
    # column identities), not the original metadata-free MiMo proposal.
    edited_plan = persisted.page_artifacts["page_1"]["question_crop_plan"]
    assert confirmed_plan["blocks"] == edited_plan["blocks"]
    assert [block["bbox_1000"] for block in confirmed_plan["blocks"]] == [
        block["bbox_1000"] for block in plan["blocks"]
    ]
    assert confirmed_plan["blocks"][0]["column_id"] == saved_column["column_id"]
    assert (
        confirmed_session.layout_cut_runs[request.action_id]["pages"]["page_1"]
        ["anchor_crop_plan"]
    ) == confirmed_plan

    with pytest.raises(HTTPException) as denied:
        grade_template.batch_grade_template_cut_candidates_get(
            session.template_id, user=SimpleNamespace(id=8)
        )
    assert denied.value.status_code == 403

    with pytest.raises(ValidationError):
        grade_template.TemplateCutCandidatesReq(
            action_id=request.action_id,
            cut_types=["answer"],
        )


@pytest.mark.anyio
async def test_new_template_cut_action_reuses_frozen_question_plan(tmp_path, monkeypatch):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, artifact = _template_session(tmp_path, "templatecut-frozen")
    calls = []
    limiter = _Limiter()

    async def fake_question_cut(image_path, *, teacher_id="system", column_mode=None):
        calls.append(Path(image_path))
        return _cut_result(artifact, request_id=f"request-{len(calls)}")

    monkeypatch.setattr(grade_template, "recognize_column_layout", fake_question_cut)
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, teacher_id: limiter)
    user = SimpleNamespace(id=7)

    first = await grade_template.batch_grade_template_cut_candidates(
        session.template_id,
        grade_template.TemplateCutCandidatesReq(action_id="template-cut-first"),
        user=user,
        db=object(),
    )
    second = await grade_template.batch_grade_template_cut_candidates(
        session.template_id,
        grade_template.TemplateCutCandidatesReq(action_id="template-cut-second"),
        user=user,
        db=object(),
    )

    assert len(calls) == 1
    assert second["action_id"] == "template-cut-second"
    assert second["pages"][0]["anchor_crop_plan"] == first["pages"][0]["anchor_crop_plan"]
    assert second["pages"][0]["modes"]["question"]["request_id"] == "request-1"

    forced = await grade_template.batch_grade_template_cut_candidates(
        session.template_id,
        grade_template.TemplateCutCandidatesReq(action_id="template-cut-forced", force=True),
        user=user,
        db=object(),
    )
    assert len(calls) == 2
    assert forced["pages"][0]["modes"]["question"]["request_id"] == "request-2"
    assert limiter.events == []


def _save_layout(session, columns, user):
    return grade_template.batch_grade_template_layout_overrides(
        session.template_id,
        grade_template.TemplateLayoutOverridesReq(
            page_index=0,
            columns=columns,
            question_anchors=[{
                "question_number": "1",
                "question_number_bbox": [50, 60, 100, 100],
            }],
        ),
        user=user,
    )


def test_layout_override_binds_column_identity_to_layout_revision(
    tmp_path, monkeypatch,
):
    """A boundary change is a new layout revision; an unchanged save is not."""
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, artifact = _template_session(tmp_path, "templatecut-revision")
    user = SimpleNamespace(id=7)
    # This case changes an existing frozen plan. A first teacher-approved plan
    # has no old column-derived solver input to invalidate (covered separately).
    previous_layout = grade_template.revise_layout_draft(
        _cut_result(artifact), column_mode="single",
        columns=[{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
    )
    session.page_artifacts["page_1"]["question_crop_plan"] = build_anchor_crop_plan(
        previous_layout, artifact_sha256=artifact["sha256"],
        expected_question_numbers=["1"],
    )
    prior_revision = session.page_artifacts["page_1"]["question_crop_plan"]["layout_context"]["layout_revision"]
    session.save_meta()

    first = _save_layout(
        session,
        [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
        user,
    )
    assert first["column_mode"] == "double"
    assert first["layout_revision"] == prior_revision + 1
    assert first["layout_changed"] is True
    first_ids = [column["column_id"] for column in first["columns"]]
    assert len(set(first_ids)) == 2
    assert all(column["layout_revision"] == prior_revision + 1 for column in first["columns"])
    assert all(column["image_sha256"] == artifact["sha256"] for column in first["columns"])
    assert [column["crop_box_px"] for column in first["columns"]] == [
        [0, 0, 450, 1200],
        [450, 0, 900, 1200],
    ]
    reloaded = TemplateSession.load_meta(session.template_id, str(tmp_path))

    # Re-saving the same boundaries keeps the revision and identities.
    same = _save_layout(
        reloaded,
        [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
        user,
    )
    assert same["layout_revision"] == first["layout_revision"]
    assert same["layout_changed"] is False
    assert [column["column_id"] for column in same["columns"]] == first_ids
    kept = TemplateSession.load_meta(session.template_id, str(tmp_path))

    # Moving the gutter invalidates every column-derived identity.
    moved = _save_layout(
        kept,
        [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 480},
            {"column_index": 2, "x1_1000": 480, "x2_1000": 1000},
        ],
        user,
    )
    assert moved["layout_revision"] == first["layout_revision"] + 1
    assert moved["layout_changed"] is True
    moved_ids = [column["column_id"] for column in moved["columns"]]
    assert set(moved_ids).isdisjoint(first_ids)
    assert all(column["layout_revision"] == moved["layout_revision"] for column in moved["columns"])
    cleared = TemplateSession.load_meta(session.template_id, str(tmp_path))
    persisted_plan = cleared.page_artifacts["page_1"]["question_crop_plan"]
    assert [block["column_id"] for block in persisted_plan["blocks"]] == moved_ids
    assert persisted_plan["layout_context"]["layout_revision"] == moved["layout_revision"]


def test_first_boundaries_over_a_columnless_plan_are_not_a_layout_change(
    tmp_path, monkeypatch,
):
    """A stored plan without trusted columns is not a layout the teacher changed."""
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, artifact = _template_session(tmp_path, "templatecut-first-plan")
    user = SimpleNamespace(id=7)
    # The artifact may already carry an unfrozen plan shell (for example after a
    # review draft). Its empty column list holds no boundaries to compare, so
    # approving the first boundaries records the teacher's layout as unchanged.
    session.page_artifacts["page_1"]["question_crop_plan"] = {
        "parameters": {"columns": []},
        "artifact_sha256": artifact["sha256"],
    }
    session.save_meta()

    saved = _save_layout(
        session,
        [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
        user,
    )
    assert saved["column_mode"] == "double"
    assert saved["layout_changed"] is False


def _review_cut_result(artifact):
    """A fail-closed double-column draft: no trusted columns, needs review."""
    from services import template_column_layout as mimo

    return mimo._review_layout_result(
        {
            "sha256": artifact["sha256"],
            "width": artifact["width"],
            "height": artifact["height"],
            "size_bytes": 100,
            "image_format": "png",
        },
        mode="double",
        diagnostics={
            "strategy": "local_gutter_v1",
            "validation": "failed",
            "confidence": 0.2,
            "confidence_level": "low",
            "reasons": ["printed_line_crosses_gutter"],
        },
        proposed_columns=None,
    )


@pytest.mark.anyio
async def test_review_layout_blocks_confirmation_until_teacher_saves_boundaries(
    tmp_path, monkeypatch,
):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, artifact = _template_session(tmp_path, "templatecut-review")
    limiter = _Limiter()

    async def fake_question_cut(image_path, *, teacher_id="system", **_kwargs):
        return _review_cut_result(artifact)

    monkeypatch.setattr(grade_template, "recognize_column_layout", fake_question_cut)
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, teacher_id: limiter)
    user = SimpleNamespace(id=7)

    # A review draft is not a crop-plan failure: the run completes, the draft
    # stays visible, and no plan is frozen from unverified boundaries.
    result = await grade_template.batch_grade_template_cut_candidates(
        session.template_id,
        grade_template.TemplateCutCandidatesReq(action_id="template-cut-review"),
        user=user,
        db=object(),
    )
    assert result["status"] == "complete"
    assert result["layout_review_pending_pages"] == [0]
    page = result["pages"][0]
    assert page["layout_review_pending"] is True
    assert page["anchor_crop_plan"] is None
    assert page["modes"]["question"]["layout_status"] == "needs_review"
    assert page["modes"]["question"]["column_layout"]["columns"] == []
    persisted = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert "question_crop_plan" not in persisted.page_artifacts["page_1"]

    confirm_req = grade_template.TemplateConfirmReq(
        template_id=session.template_id,
        category="daily",
        pages=[{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "1+1=？",
                "question_type": "fill",
                "standard_answer": "2",
            }],
        }],
    )
    with pytest.raises(HTTPException) as refused:
        grade_template.batch_grade_template_confirm(confirm_req, user=user, db=object())
    assert refused.value.status_code == 409
    assert "分栏仍需人工确认" in refused.value.detail

    saved = _save_layout(
        persisted,
        [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
        user,
    )
    assert saved["column_mode"] == "double"
    assert saved["layout_revision"] >= 1
    hydrated = grade_template.batch_grade_template_cut_candidates_get(
        session.template_id, user=user
    )
    assert hydrated["layout_review_pending_pages"] == []
    assert "layout_review_pending" not in hydrated["pages"][0]
    assert hydrated["pages"][0]["modes"]["question"]["layout_status"] == "ready"
    assert [
        (c["x1_1000"], c["x2_1000"])
        for c in hydrated["pages"][0]["anchor_crop_plan"]["parameters"]["columns"]
    ] == [(0, 500), (500, 1000)]

    confirmed = grade_template.batch_grade_template_confirm(confirm_req, user=user, db=object())
    assert confirmed["success"] is True


@pytest.mark.anyio
async def test_anchor_review_blocks_with_crop_plan_then_override_clears_candidates(
    tmp_path, monkeypatch,
):
    """Anchor review survives a crop plan and resolves only on teacher save."""
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, _artifact = _template_session(tmp_path, "templatecut-anchor-review")
    user = SimpleNamespace(id=7)
    columns = [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}]

    # Freeze a valid column plan first. Anchor review is a separate gate and
    # must remain visible even though this plan is already authoritative.
    _save_layout(session, columns, user)
    session = TemplateSession.load_meta(session.template_id, str(tmp_path))
    artifact = session.page_artifacts["page_1"]
    assert isinstance(artifact.get("question_crop_plan"), dict)

    anchor_candidates = [{
        "question_number": "1",
        "question_number_bbox": [65, 75, 115, 120],
        "confidence": 0.30,
        "human_confirmed": False,
        "confidence_source": "model",
    }]
    handwriting_candidates = [{
        "question_number": "1",
        "regions": [[525, 75, 590, 140]],
        "confidence": 0.25,
        "human_confirmed": False,
        "confidence_source": "model",
    }]
    answer_candidates = [{
        "question_number": "1",
        "regions": [[600, 150, 900, 300]],
        "confidence": 0.35,
        "human_confirmed": False,
        "confidence_source": "model",
    }]
    action_id = "anchor-review-action"
    session.layout_cut_runs[action_id] = {
        "action_id": action_id,
        "teacher_id": user.id,
        "status": "complete",
        "cut_types": ["question"],
        "image_type": "template",
        "created_at": "2026-09-03T00:00:00+00:00",
        "completed_at": "2026-09-03T00:00:01+00:00",
        "pages": {
            "page_1": {
                "page_index": 0,
                "artifact_sha256": artifact["sha256"],
                "anchor_crop_plan": artifact["question_crop_plan"],
                "modes": {"question": _cut_result(artifact)},
                # Legacy review runs may predate the explicit pending flags.
                # Candidates/diagnostics must still fail closed and be carried
                # into a fresh or forced layout action as normalized metadata.
                "question_anchor_candidates": anchor_candidates,
                "template_answer_region_candidates": answer_candidates,
                "template_handwriting_region_candidates": handwriting_candidates,
                "anchor_diagnostics": {
                    "needs_review": True,
                    "review_reason": "low_confidence",
                },
                "template_ocr_diagnostics": {
                    "needs_review": True,
                    "review_reason": "image_association_conflict",
                },
            },
        },
        "error": None,
    }
    session.layout_candidates = {
        "active_action_id": action_id,
        "updated_at": "2026-09-03T00:00:01+00:00",
    }
    session.save_meta()

    state = grade_template.batch_grade_template_cut_candidates_get(
        session.template_id,
        user=user,
    )
    assert state["layout_review_pending_pages"] == []
    assert state["anchor_review_pending_pages"] == [0]
    assert state["geometry_review_pending_pages"] == [0]
    page = state["pages"][0]
    assert page["anchor_review_pending"] is True
    assert page["geometry_review_pending"] is True
    assert page["question_anchor_candidates"] == anchor_candidates
    assert page["template_handwriting_region_candidates"] == (
        handwriting_candidates
    )
    assert page["template_answer_region_candidates"] == answer_candidates
    # The legacy fields remain artifact-owned and never silently promote the
    # low-confidence candidates.
    assert page["question_anchors"] == artifact["question_anchors"]
    assert page["template_handwriting_regions"] == (
        artifact["template_handwriting_regions"]
    )

    layout_calls = []

    async def fake_question_cut(image_path, *, teacher_id="system", **_kwargs):
        layout_calls.append(Path(image_path))
        return _cut_result(artifact, request_id=f"review-retry-{len(layout_calls)}")

    monkeypatch.setattr(grade_template, "recognize_column_layout", fake_question_cut)
    monkeypatch.setattr(
        grade_recognition,
        "_check_limits_or_raise",
        lambda db, teacher_id: _Limiter(),
    )
    # A fresh action reuses the frozen layout, while a forced action refreshes
    # MiMo. Neither action reruns anchor recognition, so both must carry the
    # same non-authoritative review draft into the active response.
    retry_action = "anchor-review-new-action"
    for force in (False, True):
        retried = await grade_template.batch_grade_template_cut_candidates(
            session.template_id,
            grade_template.TemplateCutCandidatesReq(
                action_id=retry_action,
                force=force,
            ),
            user=user,
            db=object(),
        )
        retry_page = retried["pages"][0]
        assert retried["anchor_review_pending_pages"] == [0]
        assert retried["geometry_review_pending_pages"] == [0]
        assert retry_page["anchor_review_pending"] is True
        assert retry_page["geometry_review_pending"] is True
        assert retry_page["question_anchor_candidates"] == anchor_candidates
        assert retry_page["template_handwriting_region_candidates"] == (
            handwriting_candidates
        )
        assert retry_page["template_answer_region_candidates"] == answer_candidates
        assert retry_page["anchor_diagnostics"]["review_reason"] == (
            "low_confidence"
        )
        assert retry_page["template_ocr_diagnostics"]["review_reason"] == (
            "image_association_conflict"
        )
        assert retry_page["question_anchors"] == artifact["question_anchors"]
        hydrated_retry = grade_template.batch_grade_template_cut_candidates_get(
            session.template_id,
            user=user,
        )
        assert hydrated_retry["action_id"] == retry_action
        assert hydrated_retry["pages"][0]["question_anchor_candidates"] == (
            anchor_candidates
        )
        assert hydrated_retry["pages"][0]["template_answer_region_candidates"] == (
            answer_candidates
        )
    assert len(layout_calls) == 1

    confirm_req = grade_template.TemplateConfirmReq(
        template_id=session.template_id,
        category="daily",
        pages=[{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "1+1=？",
                "question_type": "fill",
                "standard_answer": "2",
            }],
        }],
    )
    with pytest.raises(HTTPException) as refused:
        grade_template.batch_grade_template_confirm(confirm_req, user=user, db=object())
    assert refused.value.status_code == 409
    assert "题号定位或模板豁免框仍需人工确认" in refused.value.detail

    saved = grade_template.batch_grade_template_layout_overrides(
        session.template_id,
        grade_template.TemplateLayoutOverridesReq(
            page_index=0,
            columns=columns,
            question_anchors=anchor_candidates,
            template_handwriting_regions=handwriting_candidates,
        ),
        user=user,
    )
    assert saved["question_anchors"][0]["confidence"] == 1.0
    assert saved["question_anchors"][0]["human_confirmed"] is True
    assert saved["question_anchors"][0]["confidence_source"] == "teacher"
    assert saved["template_handwriting_regions"][0]["confidence"] == 1.0
    assert saved["template_handwriting_regions"][0]["human_confirmed"] is True
    assert saved["template_handwriting_regions"][0]["confidence_source"] == (
        "teacher"
    )

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    for restored_run in restored.layout_cut_runs.values():
        restored_page_run = (restored_run.get("pages") or {}).get("page_1") or {}
        for key in (
            "anchor_review_pending",
            "geometry_review_pending",
            "question_anchor_candidates",
            "template_handwriting_region_candidates",
            "anchor_diagnostics",
            "template_ocr_diagnostics",
        ):
            assert key not in restored_page_run
    restored_artifact = restored.page_artifacts["page_1"]
    assert restored_artifact["question_anchors"][0]["confidence_source"] == (
        "teacher"
    )
    assert restored_artifact["template_handwriting_regions"][0][
        "confidence_source"
    ] == "teacher"

    resolved = grade_template.batch_grade_template_cut_candidates_get(
        session.template_id,
        user=user,
    )
    assert resolved["anchor_review_pending_pages"] == []
    assert resolved["geometry_review_pending_pages"] == []
    assert resolved["pages"][0]["question_anchor_candidates"] == []
    assert resolved["pages"][0]["template_handwriting_region_candidates"] == []
    assert resolved["pages"][0]["question_anchors"][0][
        "confidence_source"
    ] == "teacher"

    confirmed = grade_template.batch_grade_template_confirm(
        confirm_req,
        user=user,
        db=object(),
    )
    assert confirmed["success"] is True


def test_template_cut_state_survives_session_reload(tmp_path):
    session, _ = _template_session(tmp_path, "templatecut2")
    session.layout_candidates = {"active_action_id": "action-123", "updated_at": "now"}
    session.layout_cut_runs = {
        "action-123": {
            "action_id": "action-123",
            "status": "complete",
            "cut_types": ["question"],
            "pages": {},
        }
    }
    session.save_meta()

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))

    assert restored.layout_candidates == session.layout_candidates
    assert restored.layout_cut_runs == session.layout_cut_runs


def test_question_insert_renumbers_later_pages_and_moves_every_artifact_box(
    tmp_path, monkeypatch,
):
    """The teacher's case, through the endpoint: a missed question on page one.

    Every collection in an artifact is keyed by question number and asserted to
    cover its page exactly, so the boxes have to be relabelled alongside the
    questions or the next validation rejects the whole template.
    """
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session = _two_page_session(tmp_path)
    user = SimpleNamespace(id=7)

    result = grade_template.batch_grade_template_question_edit(
        session.template_id,
        grade_template.TemplateQuestionEditReq(
            page_index=0,
            action="insert",
            before_question_number="1",
        ),
        user=user,
    )

    assert result["added_question_number"] == "1"
    assert result["renamed_question_numbers"] == {
        "1": "2", "2": "3", "3": "4", "4": "5",
    }
    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert [q["question_no"] for q in restored.pages[0]["questions"]] == ["1", "2", "3"]
    assert [q["question_no"] for q in restored.pages[1]["questions"]] == ["4", "5"]
    # Content followed its own question rather than staying under the old label.
    assert [q["question_text"] for q in restored.pages[1]["questions"]] == ["题3", "题4"]
    assert restored.pages[0]["questions"][0]["question_text"] == ""

    first, second = restored.page_artifacts["page_1"], restored.page_artifacts["page_2"]
    assert first["question_numbers"] == ["1", "2", "3"]
    assert second["question_numbers"] == ["4", "5"]
    for artifact in (first, second):
        numbers = artifact["question_numbers"]
        assert [a["question_number"] for a in artifact["question_anchors"]] == numbers
        assert [
            item["question_number"]
            for item in artifact["template_handwriting_regions"]
        ] == numbers
        assert artifact["question_crop_plan"]["expected_question_numbers"] == numbers
    # Legacy geometry may remain in persisted artifacts for migration, but a
    # newly inserted question never receives a synthetic number box.
    anchors = {a["question_number"]: a for a in first["question_anchors"]}
    assert anchors["2"]["question_number_bbox"] == [50, 60, 100, 100]
    assert "question_number_bbox" not in anchors["1"]
    exemptions = {
        item["question_number"]: item["regions"]
        for item in second["template_handwriting_regions"]
    }
    # Page two's "4" is the old "3" under a new label, so it carries the old
    # "3" boxes rather than anything belonging to page one's inserted question.
    assert exemptions["4"] == [[520, 70, 580, 130]]
    # The inserted question owns no exemption yet and no synthetic number box.
    first_exemptions = {
        item["question_number"]: item["regions"]
        for item in first["template_handwriting_regions"]
    }
    assert first_exemptions["1"] == []

    # Page one's own answer_map entries stay unique across the whole paper.
    numbers = [
        q["question_no"] for page in restored.pages for q in page["questions"]
    ]
    assert len(numbers) == len(set(numbers))


def test_question_delete_leaves_other_numbers_and_their_boxes_untouched(
    tmp_path, monkeypatch,
):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session = _two_page_session(tmp_path)
    user = SimpleNamespace(id=7)

    grade_template.batch_grade_template_question_edit(
        session.template_id,
        grade_template.TemplateQuestionEditReq(
            page_index=0, action="delete", question_number="1",
        ),
        user=user,
    )

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert [q["question_no"] for q in restored.pages[0]["questions"]] == ["2"]
    # Deletion does not renumber, so page two is untouched and keeps its artifact.
    assert [q["question_no"] for q in restored.pages[1]["questions"]] == ["3", "4"]
    assert restored.page_artifacts["page_1"]["question_numbers"] == ["2"]
    assert [
        a["question_number"]
        for a in restored.page_artifacts["page_1"]["question_anchors"]
    ] == ["2"]
    assert restored.page_artifacts["page_2"]["question_numbers"] == ["3", "4"]


def test_question_edit_is_refused_once_student_recognition_froze_the_template(
    tmp_path, monkeypatch,
):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session = _two_page_session(tmp_path)
    session.status = "frozen"
    session.save_meta()

    with pytest.raises(HTTPException) as excinfo:
        grade_template.batch_grade_template_question_edit(
            session.template_id,
            grade_template.TemplateQuestionEditReq(page_index=0, action="insert"),
            user=SimpleNamespace(id=7),
        )

    assert excinfo.value.status_code == 409


def test_question_edit_rejects_a_foreign_teacher(tmp_path, monkeypatch):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session = _two_page_session(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        grade_template.batch_grade_template_question_edit(
            session.template_id,
            grade_template.TemplateQuestionEditReq(page_index=0, action="insert"),
            user=SimpleNamespace(id=99),
        )

    assert excinfo.value.status_code == 403


def test_layout_override_stamps_server_owned_teacher_provenance(
    tmp_path, monkeypatch,
):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session, _artifact = _template_session(tmp_path, "templatecut-provenance")
    user = SimpleNamespace(id=7)

    saved = grade_template.batch_grade_template_layout_overrides(
        session.template_id,
        grade_template.TemplateLayoutOverridesReq(
            page_index=0,
            columns=[{
                "column_index": 1,
                "x1_1000": 0,
                "x2_1000": 1000,
            }],
            # Deliberately send model provenance; the endpoint must replace it.
            question_anchors=[{
                "question_number": "1",
                "question_number_bbox": [55, 65, 110, 110],
                "confidence": 0.1,
                "human_confirmed": False,
                "confidence_source": "model",
            }],
            template_handwriting_regions=[{
                "question_number": "1",
                "regions": [[525, 75, 590, 140]],
                "confidence": 0.1,
                "human_confirmed": False,
                "confidence_source": "model",
            }],
        ),
        user=user,
    )

    assert saved["question_anchors"][0]["confidence"] == 1.0
    assert saved["question_anchors"][0]["human_confirmed"] is True
    assert saved["question_anchors"][0]["confidence_source"] == "teacher"
    assert saved["template_handwriting_regions"][0]["confidence"] == 1.0
    assert saved["template_handwriting_regions"][0]["human_confirmed"] is True
    assert saved["template_handwriting_regions"][0]["confidence_source"] == "teacher"

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    artifact = restored.page_artifacts["page_1"]
    assert artifact["question_anchors"][0]["confidence_source"] == "teacher"
    assert artifact["template_handwriting_regions"][0]["human_confirmed"] is True


def test_question_insert_preserves_anchor_and_exemption_provenance(
    tmp_path, monkeypatch,
):
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    session = _two_page_session(tmp_path, "templateedit-provenance")
    for artifact in session.page_artifacts.values():
        for anchor in artifact.get("question_anchors") or []:
            anchor.update({
                "confidence": 0.93,
                "human_confirmed": False,
                "confidence_source": "model",
            })
        for item in artifact.get("template_handwriting_regions") or []:
            item.update({
                "confidence": 0.91,
                "human_confirmed": False,
                "confidence_source": "model",
            })
    session.save_meta()

    grade_template.batch_grade_template_question_edit(
        session.template_id,
        grade_template.TemplateQuestionEditReq(
            page_index=0,
            action="insert",
            before_question_number="1",
        ),
        user=SimpleNamespace(id=7),
    )

    restored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    first = restored.page_artifacts["page_1"]
    second = restored.page_artifacts["page_2"]
    moved_anchor = next(
        item for item in first["question_anchors"] if item["question_number"] == "2"
    )
    moved_region = next(
        item
        for item in first["template_handwriting_regions"]
        if item["question_number"] == "2"
    )
    cross_page_anchor = next(
        item for item in second["question_anchors"] if item["question_number"] == "4"
    )
    cross_page_region = next(
        item
        for item in second["template_handwriting_regions"]
        if item["question_number"] == "4"
    )
    for item, confidence in (
        (moved_anchor, 0.93),
        (moved_region, 0.91),
        (cross_page_anchor, 0.93),
        (cross_page_region, 0.91),
    ):
        assert item["confidence"] == confidence
        assert item["human_confirmed"] is False
        assert item["confidence_source"] == "model"
