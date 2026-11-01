import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from services import dashscope_budget
from services import template_column_layout as layout
from services.template_column_layout import TemplateCutError
from services.key_pool import KeyPool
from services.template_column_layout import (
    _estimated_template_payload_bytes,
    _extract_json_text,
    _strict_payload,
    normalize_column_mode,
    revise_layout_draft,
    recognize_column_layout,
    validate_layout_boundaries,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Lease:
    def __init__(self, limiter):
        self.limiter = limiter

    async def __aenter__(self):
        self.limiter.active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.limiter.active -= 1
        self.limiter.releases += 1


class _Limiter:
    def __init__(self):
        self.invalid = set()
        self.provider_failures = set()
        self.acquisitions = []
        self.active = 0
        self.releases = 0

    async def is_credential_invalid(self, **credential):
        return credential["api_key"] in self.invalid

    async def mark_credential_invalid(self, **credential):
        self.invalid.add(credential["api_key"])

    async def is_provider_failure(self, provider):
        return provider in self.provider_failures

    async def mark_provider_failure(self, provider, _cooldown_seconds):
        self.provider_failures.add(provider)

    async def clear_provider_failure(self, provider):
        self.provider_failures.discard(provider)

    async def acquire(self, **kwargs):
        self.acquisitions.append(kwargs)
        return _Lease(self)


@pytest.fixture(autouse=True)
def reset_template_capability_circuit(monkeypatch):
    monkeypatch.setattr(layout, "_template_capability_unavailable_until", 0.0)


@pytest.fixture
def layout_runtime(monkeypatch):
    pool = KeyPool(
        ["test-only-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    return SimpleNamespace(pool=pool, limiter=limiter)


@pytest.mark.anyio
async def test_template_readiness_rejects_missing_model(monkeypatch):
    """分栏模型是应用依赖（qwen3.8-flash）；就绪探针不再做 MiMo 时代的
    “-pro 文本模型”判定，缺模型时报 model_not_configured。"""
    pool = KeyPool(
        ["test-only-key"],
        max_concurrent_per_key=4,
        base_urls=["https://dashscope.test/v1"],
    )
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "")

    state = await layout.get_column_layout_readiness(_Limiter())

    assert state["configured"] is False
    assert state["ready"] is False
    assert state["status"] == "unavailable"
    assert state["reason"] == "model_not_configured"


@pytest.mark.anyio
async def test_template_readiness_reports_capability_circuit_as_fault(monkeypatch):
    pool = KeyPool(
        ["test-only-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    limiter.provider_failures.add(layout._TEMPLATE_CAPABILITY)
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    state = await layout.get_column_layout_readiness(limiter)

    assert state["configured"] is True
    assert state["ready"] is False
    assert state["status"] == "fault"
    assert state["reason"] == "runtime_circuit_open"


@pytest.mark.anyio
async def test_template_image_validation_runs_off_the_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    validation_thread = None

    def failed_validation(_image_path):
        nonlocal validation_thread
        validation_thread = threading.get_ident()
        raise TemplateCutError("IMAGE_INVALID", "synthetic invalid image")

    monkeypatch.setattr(layout, "_validate_template_image", failed_validation)

    with pytest.raises(TemplateCutError) as exc_info:
        await recognize_column_layout("ignored.png")

    assert exc_info.value.code == "IMAGE_INVALID"
    assert validation_thread is not None
    assert validation_thread != event_loop_thread


def test_strict_payload_accepts_three_columns_and_rejects_non_contiguous_bounds():
    payload = _strict_payload({
        "column_count": 3,
        "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
            {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
            {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
        ],
    })
    assert payload["column_layout"]["column_count"] == 3
    assert [candidate["template_bbox_1000"] for candidate in payload["candidates"]] == [
        [0, 0, 330, 1000],
        [330, 0, 665, 1000],
        [665, 0, 1000, 1000],
    ]
    assert all(
        candidate["coordinate_trust"] == "model_coarse"
        for candidate in payload["candidates"]
    )
    with pytest.raises(TemplateCutError, match="分栏边界无效"):
        _strict_payload({
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 490},
                {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
            ],
        })


def test_layout_mode_contract_rejects_numeric_and_defaults_to_double():
    assert normalize_column_mode() == "double"
    assert normalize_column_mode("单栏") == "single"
    with pytest.raises(TemplateCutError, match="分栏模式"):
        normalize_column_mode(2)
    with pytest.raises(TemplateCutError, match="分栏模式"):
        normalize_column_mode("quadruple")


def test_triple_boundary_contract_rejects_narrow_and_crossing_proposals():
    with pytest.raises(TemplateCutError, match="过窄"):
        validate_layout_boundaries(
            [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 50},
                {"column_index": 2, "x1_1000": 50, "x2_1000": 500},
                {"column_index": 3, "x1_1000": 500, "x2_1000": 1000},
            ],
            column_mode="triple",
        )
    with pytest.raises(TemplateCutError, match="跨越"):
        validate_layout_boundaries(
            [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
                {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
                {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
            ],
            column_mode="triple",
            evidence={"question_boxes": [[300, 100, 350, 200]]},
        )


@pytest.mark.anyio
async def test_single_mode_skips_model_and_returns_whole_page_draft(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "single.png"
    Image.new("RGB", (1001, 1400), "white").save(image_path)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("single layout must not resolve or call the layout model")

    monkeypatch.setattr(layout, "_resolve_template_key_pool", forbidden)
    result = await recognize_column_layout(image_path, column_mode="single")
    assert calls == []
    assert result["layout_status"] == "ready"
    assert result["layout_diagnostics"]["strategy"] == "single_direct"
    column = result["column_layout"]["columns"][0]
    assert column["column_id"]
    assert column["crop_left"] == 0
    assert column["crop_right"] == 1001
    assert column["source_width"] == 1001
    assert column["layout_revision"] == result["layout_revision"]


@pytest.mark.anyio
async def test_explicit_double_low_confidence_falls_back_to_model(
    tmp_path, monkeypatch, layout_runtime,
):
    """2026-10-03 契约变更（用户裁定）：本地双栏证据不达标不再锁死在
    needs_review（旧测试断言的正是被替换的旧契约），改由 qwen3.8-flash
    （官方关闭思考组合）给出边界；本地像素证据降级为诊断记录。"""
    image_path = tmp_path / "printed-line.png"
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((70, y, 1130, y), fill=35, width=3)
    image.convert("RGB").save(image_path)
    observed = {}
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "qwen-double-1",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 455},
                    {"column_index": 2, "x1_1000": 455, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="double",
        )

    assert observed["payload"]["enable_thinking"] is False
    assert observed["payload"]["reasoning_effort"] == "none"
    assert "教师已经选择双栏" in observed["payload"]["messages"][0]["content"][0]["text"]
    assert result["layout_status"] == "ready"
    assert result["usable_for_ocr"] is True
    assert result["column_layout"]["boundaries_1000"] == [455]
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["strategy"] == "model_boundary_proposal"
    assert diagnostics["gate_role"] == "diagnostics_only"
    assert diagnostics["evidence_version"] == layout._DOUBLE_EVIDENCE_VERSION


@pytest.mark.anyio
async def test_explicit_double_model_outage_raises_instead_of_review_draft(
    tmp_path, monkeypatch,
):
    """模型不可用按传输错误上抛：provider 故障不伪装成复核草稿。"""
    image_path = tmp_path / "printed-line.png"
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((70, y, 1130, y), fill=35, width=3)
    image.convert("RGB").save(image_path)
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: None)

    with pytest.raises(TemplateCutError) as exc_info:
        await recognize_column_layout(image_path, column_mode="double")

    assert exc_info.value.code == "CREDENTIAL_UNAVAILABLE"


@pytest.mark.anyio
async def test_triple_model_draft_has_actual_pixel_endpoints_and_revision(
    tmp_path, monkeypatch, layout_runtime,
):
    image_path = tmp_path / "triple.png"
    _synthetic_three_column_page(image_path)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")
    observed = {}

    async def handler(_request):
        observed["prompt"] = json.loads(_request.content)["messages"][0]["content"][0]["text"]
        return httpx.Response(200, json={
            "id": "triple-contract",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 333},
                    {"column_index": 2, "x1_1000": 333, "x2_1000": 667},
                    {"column_index": 3, "x1_1000": 667, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="triple",
            layout_revision=4,
        )
    assert result["layout_status"] == "ready"
    assert result["layout_revision"] == 4
    columns = result["column_layout"]["columns"]
    assert [(item["crop_left"], item["crop_right"]) for item in columns] == [
        (0, round(333 * 1001 / 1000)),
        (round(333 * 1001 / 1000), round(667 * 1001 / 1000)),
        (round(667 * 1001 / 1000), 1001),
    ]
    assert len({item["column_id"] for item in columns}) == 3
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["evidence_version"] == layout._TRIPLE_EVIDENCE_VERSION
    assert diagnostics["thresholds"] == layout._TRIPLE_EVIDENCE_THRESHOLDS
    assert diagnostics["validation"] == "passed"
    assert len(diagnostics["columns"]) == 3
    assert len(diagnostics["seams"]) == 2
    assert "教师已经选择三栏" in observed["prompt"]
    assert "不要自行选择栏数" in observed["prompt"]
    assert '"column_count":3' in observed["prompt"]
    assert '"column_count":2' not in observed["prompt"]
    assert "选择能让每道题及其作答区域完整落在同一栏内的最少栏数" not in observed["prompt"]


@pytest.mark.anyio
async def test_triple_invalid_provider_boundary_returns_review_without_crops(
    tmp_path, monkeypatch, layout_runtime,
):
    image_path = tmp_path / "triple-invalid.png"
    Image.new("RGB", (1001, 1400), "white").save(image_path)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 40},
                    {"column_index": 2, "x1_1000": 40, "x2_1000": 600},
                    {"column_index": 3, "x1_1000": 600, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="triple",
        )
    assert result["layout_status"] == "needs_review"
    assert result["usable_for_ocr"] is False
    assert result["column_layout"]["columns"] == []
    assert result["pages"][0]["candidates"] == []


@pytest.mark.anyio
async def test_triple_blank_column_returns_review_with_content_evidence(
    tmp_path, monkeypatch, layout_runtime,
):
    image_path = tmp_path / "triple-blank-middle.png"
    _synthetic_three_column_page(image_path, blank_column=2)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 333},
                    {"column_index": 2, "x1_1000": 333, "x2_1000": 667},
                    {"column_index": 3, "x1_1000": 667, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="triple",
        )
    assert result["layout_status"] == "needs_review"
    assert result["usable_for_ocr"] is False
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["evidence_version"] == layout._TRIPLE_EVIDENCE_VERSION
    assert diagnostics["thresholds"] == layout._TRIPLE_EVIDENCE_THRESHOLDS
    assert "provider_boundary_rejected" in diagnostics["reasons"]
    assert "insufficient_column_content:col_2" in diagnostics["reasons"]
    # The geometrically valid proposal stays visible to the teacher as an
    # explicitly unusable candidate rather than being dropped or trusted.
    assert len(result["column_layout"]["columns"]) == 3
    candidates = result["pages"][0]["candidates"]
    assert len(candidates) == 3
    assert all(item["usable_for_original_overlay"] is False for item in candidates)


def test_teacher_boundary_edit_increments_revision_and_invalidates_column_ids():
    source = {
        "sha256": "a" * 64,
        "width": 1001,
        "height": 1400,
        "size_bytes": 1,
        "image_format": "png",
    }
    first = layout._build_layout_result(
        source,
        layout._payload_from_columns(
            [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
            ],
            source_key="test",
            trust="test",
        ),
        provider="test",
        column_mode="double",
    )
    second = revise_layout_draft(
        first,
        column_mode="double",
        boundaries=[0, 480, 1000],
    )
    assert second["layout_revision"] == first["layout_revision"] + 1
    assert second["column_layout"]["columns"][0]["column_id"] != (
        first["column_layout"]["columns"][0]["column_id"]
    )


def test_first_teacher_edit_of_a_source_only_stub_starts_at_revision_one():
    stub = {"source": {"sha256": "c" * 64, "width": 900, "height": 1200}}
    first = revise_layout_draft(stub, column_mode="double", boundaries=[0, 500, 1000])
    assert first["layout_revision"] == 1
    assert first["layout_status"] == "ready"
    assert [c["crop_box_px"] for c in first["column_layout"]["columns"]] == [
        [0, 0, 450, 1200],
        [450, 0, 900, 1200],
    ]


def test_review_draft_falls_back_to_one_whole_page_input_and_stays_review():
    source = {
        "sha256": "d" * 64,
        "width": 1001,
        "height": 1400,
        "size_bytes": 1,
        "image_format": "png",
    }
    review = layout._review_layout_result(
        source,
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
    assert layout.layout_needs_review(review) is True
    assert review["column_layout"]["columns"] == []
    # Legacy drafts without a status remain usable.
    assert layout.layout_needs_review({"column_layout": {"columns": []}}) is False
    assert layout.layout_needs_review({"usable_for_ocr": False}) is True

    fallback = layout.whole_page_fallback_layout(review)
    assert fallback["fallback_for_review"] is True
    assert fallback["layout_status"] == "ready"
    assert fallback["column_mode"] == "single"
    assert layout.layout_needs_review(fallback) is False
    columns = fallback["column_layout"]["columns"]
    assert len(columns) == 1
    assert (columns[0]["x1_1000"], columns[0]["x2_1000"]) == (0, 1000)
    assert columns[0]["crop_box_px"] == [0, 0, 1001, 1400]
    assert columns[0]["image_sha256"] == "d" * 64
    diagnostics = fallback["layout_diagnostics"]
    assert diagnostics["strategy"] == "whole_page_fallback"
    assert diagnostics["review_mode"] == "double"
    assert diagnostics["review_reasons"] == ["printed_line_crosses_gutter"]
    # The fallback never rewrites the teacher-facing review draft.
    assert review["layout_status"] == "needs_review"
    assert review["usable_for_ocr"] is False

    with pytest.raises(TemplateCutError):
        layout.whole_page_fallback_layout({"column_layout": {"columns": []}})


def _synthetic_two_column_page(path, *, across_gutter=False, footer_rule=False):
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((70, y, 530, y), fill=35, width=3)
        draw.line((670, y, 1130, y), fill=35, width=3)
    if across_gutter:
        draw.line((70, 420, 1130, 420), fill=35, width=3)
    if footer_rule:
        draw.line((20, 746, 1180, 746), fill=35, width=7)
    image.convert("RGB").save(path)


def _synthetic_unequal_two_column_page(path):
    """A two-column page whose real gutter is visibly off centre.

    The left text block ends near x=450 while the right block starts near
    x=630.  A nominal 500/500 split would therefore clip the left block and
    leave excess whitespace on the right; the local gate must keep this page
    in teacher review instead of silently freezing an equal split.
    """
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((60, y, 450, y), fill=35, width=3)
        draw.line((630, y, 1130, y), fill=35, width=3)
    image.convert("RGB").save(path)


def _synthetic_three_column_page(path, *, blank_column=None):
    """A 1001x1400 page with printed text bands in three columns.

    Seams sit at 333/667 (1000 units); ``blank_column`` leaves one column
    empty so the content gate has something to reject.
    """
    image = Image.new("L", (1001, 1400), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    bands = {1: (40, 290), 2: (375, 625), 3: (710, 960)}
    for column_index, (left, right) in bands.items():
        if column_index == blank_column:
            continue
        for y in range(250, 1300, 10):
            draw.line((left, y, right, y), fill=35, width=3)
    image.convert("RGB").save(path)


def test_local_gutter_fast_path_accepts_clear_two_column_page(tmp_path):
    image_path = tmp_path / "two-column.png"
    _synthetic_two_column_page(image_path)

    result = layout._infer_local_two_column_payload(image_path)

    assert result is not None
    payload, diagnostics = result
    assert payload["column_layout"] == {
        "column_count": 2,
        "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
    }
    assert diagnostics["strategy"] == "local_gutter_v1"
    assert all(
        candidate["coordinate_trust"] == "local_high_confidence"
        for candidate in payload["candidates"]
    )


def test_local_gutter_fast_path_fails_closed_for_single_column_page(tmp_path):
    image_path = tmp_path / "single-column.png"
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((70, y, 1130, y), fill=35, width=3)
    image.convert("RGB").save(image_path)

    assert layout._infer_local_two_column_payload(image_path) is None


def test_local_gutter_does_not_snap_a_narrow_off_center_boundary(tmp_path):
    image_path = tmp_path / "offset-gutter.png"
    image = Image.new("L", (1200, 800), 245)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(150, 730, 8):
        draw.line((70, y, 520, y), fill=35, width=3)
        draw.line((650, y, 1130, y), fill=35, width=3)
    image.convert("RGB").save(image_path)

    result = layout._infer_local_two_column_payload(image_path)

    assert result is not None
    split = result[0]["column_layout"]["columns"][0]["x2_1000"]
    assert 425 <= split <= 490


@pytest.mark.anyio
async def test_unequal_two_column_page_uses_model_boundary_not_equal_split(
    tmp_path, monkeypatch, layout_runtime,
):
    """不对称双栏页绝不静默均分（旧契约保留的意图）：模型给出真实偏心缝，
    本地证据的 off_center_boundary 判定仍留档可审计（2026-10-03 契约变更）。"""
    image_path = tmp_path / "unequal-two-column.png"
    _synthetic_unequal_two_column_page(image_path)
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "qwen-unequal-1",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 450},
                    {"column_index": 2, "x1_1000": 450, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="double",
        )

    assert result["layout_status"] == "ready"
    assert result["column_layout"]["boundaries_1000"] == [450]
    assert result["column_layout"]["boundaries_1000"] != [500]
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["strategy"] == "model_boundary_proposal"
    assert diagnostics["gate_role"] == "diagnostics_only"
    assert "off_center_boundary" in diagnostics["reasons"]


@pytest.mark.anyio
async def test_double_gate_evidence_persists_its_version_and_thresholds(
    tmp_path, monkeypatch, layout_runtime,
):
    """A persisted ready/needs_review decision must be auditable against the
    exact scoring rules that produced it, not a later release's constants."""
    clear = tmp_path / "two-column.png"
    _synthetic_two_column_page(clear)
    evidence = layout._inspect_local_double_evidence(clear)
    assert evidence["evidence_version"] == layout._DOUBLE_EVIDENCE_VERSION
    assert evidence["thresholds"] == layout._DOUBLE_EVIDENCE_THRESHOLDS
    assert evidence["thresholds"] is not layout._DOUBLE_EVIDENCE_THRESHOLDS
    assert evidence["validation"] == "passed"
    # Every gate the code applies is named in the persisted rule set.
    assert {
        "vertical_peak_density_max", "seam_active_rows_max", "seam_density_floor",
        "seam_to_side_ratio_max", "cross_line_fraction_max", "side_density_min",
        "side_active_rows_min", "left_right_ratio_max", "center_offset_1000_max",
        "broad_valley_1000_min", "broad_valley_1000_max", "broad_valley_density_max",
        "cross_line_scan_bottom_fraction", "min_column_width_1000",
    } <= set(evidence["thresholds"])

    # The same rule set travels with a model-accepted draft and a local draft.
    crossed = tmp_path / "crossed.png"
    _synthetic_two_column_page(crossed, across_gutter=True)
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request):
        return httpx.Response(200, json={
            "id": "qwen-crossed-1",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model_backed = await recognize_column_layout(
            crossed, client=client, column_mode="double",
        )
    assert model_backed["layout_status"] == "ready"
    assert model_backed["layout_diagnostics"]["evidence_version"] == layout._DOUBLE_EVIDENCE_VERSION
    assert model_backed["layout_diagnostics"]["thresholds"] == layout._DOUBLE_EVIDENCE_THRESHOLDS
    assert model_backed["layout_diagnostics"]["cross_line_fraction"] > (
        model_backed["layout_diagnostics"]["thresholds"]["cross_line_fraction_max"]
    )
    assert model_backed["layout_diagnostics"]["gate_role"] == "diagnostics_only"

    ready = await recognize_column_layout(clear, column_mode="double")
    assert ready["layout_status"] == "ready"
    assert ready["layout_diagnostics"]["thresholds"] == layout._DOUBLE_EVIDENCE_THRESHOLDS


@pytest.mark.anyio
async def test_triple_seam_evidence_is_advisory_not_a_veto(
    tmp_path, monkeypatch, layout_runtime,
):
    """2026-10-03 契约变更（用户裁定）：seam/crossing 类像素证据只留档不否决。
    真值对照（qwen-column-split-20261003）中合法边界落在成对印刷细线之间的
    空白通道，旧 seam_not_blank 会把合格边界误拒进复核。空栏（模型编造栏）
    的 insufficient_column_content 仍然阻断。"""
    image_path = tmp_path / "three-column.png"
    _synthetic_three_column_page(image_path)
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    def noisy_gate(_image_path, _columns):
        return {
            "evidence_version": layout._TRIPLE_EVIDENCE_VERSION,
            "strategy": "local_triple_content_v1",
            "thresholds": dict(layout._TRIPLE_EVIDENCE_THRESHOLDS),
            "confidence": 0.3,
            "confidence_level": "low",
            "validation": "failed",
            "reasons": ["seam_not_blank:333", "printed_line_crosses_gutter"],
            "columns": [],
            "seams": [],
        }

    monkeypatch.setattr(layout, "_inspect_local_triple_evidence", noisy_gate)

    async def handler(_request):
        return httpx.Response(200, json={
            "id": "qwen-triple-1",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 333},
                    {"column_index": 2, "x1_1000": 333, "x2_1000": 667},
                    {"column_index": 3, "x1_1000": 667, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            column_mode="triple",
        )

    assert result["layout_status"] == "ready"
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["gate_role"] == "diagnostics_only"
    assert "seam_not_blank:333" in diagnostics["reasons"]


@pytest.mark.anyio
async def test_double_gate_ignores_canonicalized_page_footer_rule(tmp_path):
    """A page-bottom structural rule is not a question crossing columns."""
    image_path = tmp_path / "two-column-with-footer.png"
    _synthetic_two_column_page(image_path, footer_rule=True)

    result = await recognize_column_layout(image_path, column_mode="double")

    assert result["layout_status"] == "ready"
    diagnostics = result["layout_diagnostics"]
    assert diagnostics["validation"] == "passed"
    assert diagnostics["cross_line_fraction"] == 0.0
    assert diagnostics["cross_line_scan_bottom_y"] == 720


@pytest.mark.anyio
async def test_production_layout_uses_local_fast_path_before_model_credentials(tmp_path):
    image_path = tmp_path / "two-column.png"
    _synthetic_two_column_page(image_path)

    result = await recognize_column_layout(image_path)

    assert result["provider"] == "local_projection"
    assert result["layout_diagnostics"]["strategy"] == "local_gutter_v1"
    assert result["column_layout"]["column_count"] == 2


def test_strict_payload_normalizes_integer_floats_but_rejects_fractional_bounds():
    payload = _strict_payload({
        "column_count": 2.0,
        "columns": [
            {"column_index": 1.0, "x1_1000": 0.0, "x2_1000": 500.0},
            {"column_index": 2.0, "x1_1000": 500.0, "x2_1000": 1000.0},
        ],
    })

    assert payload["column_layout"] == {
        "column_count": 2,
        "columns": [
            {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
            {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
        ],
    }

    with pytest.raises(TemplateCutError, match="分栏边界无效"):
        _strict_payload({
            "column_count": 2,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 500.5},
                {"column_index": 2, "x1_1000": 500.5, "x2_1000": 1000},
            ],
        })


def test_template_layout_prompt_matches_frozen_pipeline_contract():
    assert "已经完成旋转、透视和页面边界矫正" in layout._PROMPT
    assert "单栏、双栏还是三栏" in layout._PROMPT
    assert "冻结" in layout._PROMPT and "复用于同一页型" in layout._PROMPT
    assert "不能按固定的 500/333 像素猜测" in layout._PROMPT
    assert "不要识别题干、答案或学生笔迹" in layout._PROMPT
    assert "教师已经选择双栏" in layout._DOUBLE_PROMPT
    assert "必须返回恰好两栏和一条实际边界" in layout._DOUBLE_PROMPT
    assert "教师已经选择三栏" in layout._TRIPLE_PROMPT
    assert "必须返回恰好三栏和两条实际边界" in layout._TRIPLE_PROMPT


def test_extract_json_text_accepts_fence_or_short_preamble():
    fenced = _extract_json_text({"content": "```json\n{\"anchors\": []}\n```"})
    preamble = _extract_json_text({"content": "结果如下：{\"anchors\": []}"})
    assert json.loads(fenced) == {"anchors": []}
    assert json.loads(preamble) == {"anchors": []}


@pytest.mark.anyio
async def test_recognize_column_layout_uses_vision_contract(
    tmp_path, monkeypatch, layout_runtime,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (1000, 1400), "white").save(image_path)
    observed = {}
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "layout-request-1",
            "choices": [{"message": {"content": json.dumps({
                "column_count": 3,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 330},
                    {"column_index": 2, "x1_1000": 330, "x2_1000": 665},
                    {"column_index": 3, "x1_1000": 665, "x2_1000": 1000},
                ],
            })}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(
            image_path,
            client=client,
            teacher_id="teacher-7",
        )

    payload = observed["payload"]
    assert payload["model"] == "qwen3.8-flash"
    assert payload["temperature"] == 0
    # Qwen 官方关闭组合（两键同发）：分栏固定不思考；非流式是官方允许组合
    # （官方只禁止“思考开启 + 非流式”）。
    assert payload["enable_thinking"] is False
    assert payload["reasoning_effort"] == "none"
    assert "thinking" not in payload and "thinking_budget" not in payload
    assert payload["stream"] is False
    assert payload["messages"][0]["content"][1]["image_url"]["detail"] == "high"
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert observed["authorization"] == "Bearer test-only-key"
    lease_call = layout_runtime.limiter.acquisitions[0]
    assert lease_call["provider"] == "dashscope"
    assert lease_call["teacher_id"] == "teacher-7"
    assert lease_call["global_capacity"] == layout._template_cut_route().global_concurrency
    # The shared credential may also be in use by student OCR and grading;
    # layout's local KeyPool window must not replace the aggregate Redis cap.
    assert lease_call["credential_capacity"] == layout.DASHSCOPE_GLOBAL_CONCURRENCY
    assert layout_runtime.limiter.releases == 1
    assert result["provider"] == "dashscope"
    assert result["request_id"] == "layout-request-1"
    assert result["summary"] == {
        "total": 3,
        "original": 3,
        "rectified_untrusted": 0,
        "invalid": 0,
        "missing": 0,
    }
    assert [
        candidate["template_bbox_1000"]
        for candidate in result["pages"][0]["candidates"]
    ] == [[0, 0, 330, 1000], [330, 0, 665, 1000], [665, 0, 1000, 1000]]
    assert result["column_layout"]["column_count"] == 3
    assert str(image_path) not in json.dumps(result, ensure_ascii=False)


@pytest.mark.anyio
async def test_recognize_column_layout_prefers_json_reasoning_content(
    tmp_path, monkeypatch, layout_runtime,
):
    image_path = tmp_path / "teacher-template.jpg"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_API_KEY", "test-only-key")
    monkeypatch.setattr("services.template_column_layout.TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "分析完成",
                "reasoning_content": json.dumps({
                    "column_count": 1,
                    "columns": [
                        {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
                    ],
                }),
            }}]
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["original"] == 1


def _valid_response(request_id="request-ok"):
    return {
        "id": request_id,
        "choices": [{"message": {"content": json.dumps({
            "column_count": 1,
            "columns": [
                {"column_index": 1, "x1_1000": 0, "x2_1000": 1000}
            ],
        })}}],
    }


def test_rate_budget_reserves_estimated_tokens_not_base64_bytes():
    """RPM/TPM 预留按计费 token 估算（2026-10-03 实测踩坑）：图片 Base64
    字节数不是 token 数，按字节预留会把 5MB 图片顶成 600 万 token 预留，
    合法请求永远等不到额度。"""
    request_json = {
        "model": "qwen3.8-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "分栏提示词"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 6_500_000}},
            ],
        }],
    }

    reserved = dashscope_budget.estimate_request_tokens(request_json, 8192)

    assert 8192 < reserved < 20000


def test_template_payload_estimate_covers_serialized_http_body():
    raw_bytes = 10
    encoded_bytes = 16

    assert _estimated_template_payload_bytes(raw_bytes) == (
        raw_bytes + 3 * encoded_bytes
    )


@pytest.mark.anyio
async def test_template_payload_uses_shared_byte_budget_until_http_finishes(
    tmp_path, monkeypatch, layout_runtime,
):
    from services import ocr_service

    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    state = {"active": False, "reserved": 0}

    class Reservation:
        async def __aenter__(self):
            state["active"] = True

        async def __aexit__(self, _exc_type, _exc, _tb):
            state["active"] = False

    class Budget:
        def reserve(self, amount):
            state["reserved"] = amount
            return Reservation()

    monkeypatch.setattr(ocr_service, "_get_local_ocr_payload_budget", lambda: Budget())

    async def handler(_request):
        assert state["active"] is True
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["total"] == 1
    assert state["reserved"] == _estimated_template_payload_bytes(
        image_path.stat().st_size
    )
    assert state["active"] is False


@pytest.mark.anyio
async def test_template_payload_over_budget_fails_before_provider_call(
    tmp_path, monkeypatch, layout_runtime,
):
    from services import ocr_service

    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    provider_calls = 0

    class Reservation:
        async def __aenter__(self):
            raise ValueError("over budget")

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

    class Budget:
        def reserve(self, _amount):
            return Reservation()

    monkeypatch.setattr(ocr_service, "_get_local_ocr_payload_budget", lambda: Budget())

    async def handler(_request):
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)

    assert exc_info.value.code == "IMAGE_MEMORY_LIMIT"
    assert provider_calls == 0


@pytest.mark.anyio
async def test_template_does_not_fallback_to_generic_llm_pool(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["pool-key-a", "pool-key-b"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1", "https://layout.test/v1"],
    )
    limiter = _Limiter()
    from services import analyzer

    monkeypatch.delenv("TEMPLATE_CUT_API_KEY", raising=False)
    monkeypatch.delenv("TEMPLATE_CUT_API_KEYS", raising=False)
    monkeypatch.setenv("LLM_API_KEYS", "pool-key-a,pool-key-b")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEYS", "")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")
    monkeypatch.setattr(analyzer, "_llm_pool", pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    seen = []

    async def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)

    assert exc_info.value.code == "CREDENTIAL_UNAVAILABLE"
    assert seen == []


@pytest.mark.anyio
async def test_unauthorized_key_is_disabled_and_next_key_is_used(
    tmp_path, monkeypatch, caplog,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["raw-bad-secret", "raw-good-secret"],
        max_concurrent_per_key=4,
        weights=[100, 1],
        base_urls=["https://layout.test/v1", "https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    seen = []

    async def handler(request):
        key = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == "raw-bad-secret":
            return httpx.Response(401, text="rejected")
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["total"] == 1
    assert seen == ["raw-bad-secret", "raw-good-secret"]
    assert limiter.invalid == {"raw-bad-secret"}
    assert [
        call["global_capacity"] for call in limiter.acquisitions
    ] == [layout._template_cut_route().global_concurrency] * 2
    assert pool.enabled_key_count() == 1
    assert all(item["in_flight"] == 0 for item in pool.status())
    assert "raw-bad-secret" not in caplog.text
    assert "raw-bad-secret" not in str(pool.status())


@pytest.mark.anyio
async def test_rotated_single_key_gets_a_fresh_pool_after_permanent_disable(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    limiter = _Limiter()
    monkeypatch.setenv("TEMPLATE_CUT_API_KEY", "configured-for-test")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "expired-key")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEYS", "")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_BASE_URL", "https://layout.test/v1")
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")
    monkeypatch.setattr(layout, "_template_pool", None)
    monkeypatch.setattr(layout, "_template_pool_signature", "")
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    seen = []

    async def handler(request):
        key = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == "expired-key":
            return httpx.Response(403, text="expired")
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)
        assert exc_info.value.code == "CREDENTIAL_UNAVAILABLE"

        monkeypatch.setattr(layout, "TEMPLATE_CUT_API_KEY", "rotated-key")
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["total"] == 1
    assert seen == ["expired-key", "rotated-key"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 422])
async def test_request_http_error_does_not_open_template_capability_circuit(
    tmp_path, monkeypatch, status,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["request-error-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request):
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)

    readiness = await layout.get_column_layout_readiness(limiter)
    assert exc_info.value.retryable is False
    assert readiness["status"] == "available"
    assert layout._TEMPLATE_CAPABILITY not in limiter.provider_failures
    assert pool.status()[0]["in_flight"] == 0


@pytest.mark.anyio
async def test_endpoint_error_opens_template_capability_fault_without_leaking_slot(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["endpoint-error-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")

    async def handler(_request):
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError):
            await recognize_column_layout(image_path, client=client)

    readiness = await layout.get_column_layout_readiness(limiter)
    assert readiness["status"] == "fault"
    assert readiness["reason"] == "runtime_circuit_open"
    assert layout._TEMPLATE_CAPABILITY in limiter.provider_failures
    assert pool.status()[0]["in_flight"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 503])
async def test_transient_http_failures_have_bounded_retries_and_release_capacity(
    tmp_path, monkeypatch, status,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    keys = (
        [f"transient-key-{index}" for index in range(layout._MAX_TRANSIENT_ATTEMPTS)]
        if status == 429
        else ["transient-key"]
    )
    pool = KeyPool(
        keys,
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"] * len(keys),
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(layout.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        layout,
        "exponential_retry_delay",
        lambda *_args, **_kwargs: 0.321,
    )
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"retry-after": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)

    assert exc_info.value.retryable is True
    assert calls == layout._MAX_TRANSIENT_ATTEMPTS
    assert sleeps == [0.321] * (layout._MAX_TRANSIENT_ATTEMPTS - 1)
    assert limiter.releases == layout._MAX_TRANSIENT_ATTEMPTS
    assert all(item["in_flight"] == 0 for item in pool.status())
    assert (layout._TEMPLATE_CAPABILITY in limiter.provider_failures) is (status == 503)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 503])
async def test_long_retry_after_fails_fast_without_retrying_early(
    tmp_path, monkeypatch, status,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["limited-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )

    class RecordingLimiter(_Limiter):
        def __init__(self):
            super().__init__()
            self.cooldowns = []

        async def credential_cooldown_seconds(self, **_credential):
            return 0.0

        async def mark_credential_cooldown(self, **credential):
            self.cooldowns.append(credential["cooldown_seconds"])

    limiter = RecordingLimiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(layout.asyncio, "sleep", record_sleep)
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"retry-after": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path, client=client)

    assert exc_info.value.retryable is True
    assert calls == 1
    assert sleeps == []
    assert limiter.cooldowns == ([3600.0] if status == 429 else [])
    assert pool.status()[0]["in_flight"] == 0
    assert (layout._TEMPLATE_CAPABILITY in limiter.provider_failures) is (status == 503)


def test_retry_after_http_date_is_supported_and_bounded_by_retry_policy(monkeypatch):
    monkeypatch.setattr(
        "services.retry_backoff.time.time",
        lambda: 1_445_412_470.0,
    )
    response = httpx.Response(
        429,
        headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )

    assert layout._retry_after_seconds(response) == 10.0


@pytest.mark.anyio
async def test_template_key_pool_capacity_wait_has_finite_deadline(
    tmp_path, monkeypatch,
):
    from services.model_concurrency import NoopModelLimiter

    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["busy-key"],
        max_concurrent_per_key=1,
        base_urls=["https://layout.test/v1"],
    )
    key, _, _ = pool.get_key()
    assert key == "busy-key"
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "_KEY_ACQUIRE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(layout, "TEMPLATE_CUT_MODEL", "qwen3.8-flash")
    monkeypatch.setattr(layout, "get_model_limiter", lambda: NoopModelLimiter())

    try:
        with pytest.raises(TemplateCutError) as exc_info:
            await recognize_column_layout(image_path)
    finally:
        pool.release(key)

    assert exc_info.value.code == "PROVIDER_BUSY"
    assert exc_info.value.retryable is True


@pytest.mark.anyio
async def test_transport_retry_recovers_without_leaking_capacity(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["transport-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(layout.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        layout,
        "exponential_retry_delay",
        lambda *_args, **_kwargs: 0.123,
    )
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise httpx.ReadError("connection dropped", request=request)
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["total"] == 1
    assert calls == 2
    assert sleeps == [0.123]
    assert limiter.releases == 2
    assert pool.status()[0]["in_flight"] == 0


@pytest.mark.anyio
async def test_invalid_success_payload_retries_without_leaking_capacity(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["invalid-payload-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(layout.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        layout,
        "exponential_retry_delay",
        lambda *_args, **_kwargs: 0.234,
    )
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await recognize_column_layout(image_path, client=client)

    assert result["summary"]["total"] == 1
    assert calls == 2
    assert sleeps == [0.234]
    assert limiter.releases == 2
    assert pool.status()[0]["in_flight"] == 0


@pytest.mark.anyio
async def test_cancellation_propagates_and_releases_pool_and_shared_lease(
    tmp_path, monkeypatch,
):
    image_path = tmp_path / "teacher-template.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    pool = KeyPool(
        ["cancel-key"],
        max_concurrent_per_key=4,
        base_urls=["https://layout.test/v1"],
    )
    limiter = _Limiter()
    monkeypatch.setattr(layout, "_resolve_template_key_pool", lambda: pool)
    monkeypatch.setattr(layout, "get_model_limiter", lambda: limiter)
    started = asyncio.Event()

    class BlockingClient:
        async def post(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        recognize_column_layout(image_path, client=BlockingClient())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.active == 0
    assert limiter.releases == 1
    assert pool.status()[0]["in_flight"] == 0


def test_template_cut_route_uses_the_group_base_url(monkeypatch):
    """设置页存的组级 base_url 必须真正生效。

    回归：`_template_cut_route()` 曾把 profile_base_url 算出来又丢掉，只返回
    TEMPLATE_CUT_BASE_URL，导致组里填的自定义端点被发到环境变量地址。
    """
    from services import model_profiles, template_column_layout

    monkeypatch.setattr(
        model_profiles,
        "resolve",
        lambda group: {
            "api_key": "sk-group-key",
            "base_url": "https://example.test/v1",
            "model": "qwen3.8-max",
            "concurrency": 4,
            "source": "group",
        },
    )

    route = template_column_layout._template_cut_route()

    assert route.base_url == "https://example.test/v1"
    assert route.api_key == "sk-group-key"
