"""Unit contracts for the lazy-loaded single-context CV-CUDA lane."""
from __future__ import annotations

import pytest
import numpy as np

from worker import accelerator_client
from worker.cvcuda_batch_processor import (
    MAX_TEMPLATE_CACHE_ENTRIES,
    CVCudaBatchProcessor,
)


def test_gpu_processor_bounds_single_context_microbatch_to_six():
    assert CVCudaBatchProcessor(batch_size=2).batch_size == 2
    assert CVCudaBatchProcessor(batch_size=6).batch_size == 6
    with pytest.raises(ValueError, match="between 1 and 6"):
        CVCudaBatchProcessor(batch_size=8)


def test_oversized_input_is_rejected_before_nvimgcodec_import(monkeypatch, tmp_path):
    import worker.cvcuda_batch_processor as module

    source = tmp_path / "oversized.png"
    source.write_bytes(b"header-only-test")

    class OversizedImage:
        size = (5_001, 5_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        module.Image,
        "open",
        lambda *_args, **_kwargs: OversizedImage(),
    )

    with pytest.raises(RuntimeError, match="image_dimensions_invalid"):
        CVCudaBatchProcessor(batch_size=2)._decode(source)


def test_full_resolution_decode_is_resized_before_batch_residency(monkeypatch, tmp_path):
    processor = CVCudaBatchProcessor(batch_size=2, max_edge=60)
    decoded = np.zeros((120, 240, 3), dtype=np.uint8)
    monkeypatch.setattr(processor, "_decode", lambda _path: decoded)

    working, original_shape, scale = processor._prepare_image(tmp_path / "source.png")

    assert original_shape == (120, 240)
    assert working.shape == (30, 60, 3)
    assert scale == 0.25


def test_accelerator_client_routes_gpu_backend_without_importing_cuda_on_cpu(monkeypatch, tmp_path):
    calls = []

    class FakeProcessor:
        def __init__(self, batch_size):
            assert batch_size == 2

        def process_batch(self, items):
            calls.extend(items)
            return [{"status": "succeeded"}]

    import worker.cvcuda_batch_processor as module
    monkeypatch.setattr(module, "CVCudaBatchProcessor", FakeProcessor)
    args = type("Args", (), {"backend": "cvcuda_single_context_v2"})()
    job = {
        "template_sha256": "a" * 64,
        "layout_snapshot": {"page_type_id": "page_1"},
        "layout_snapshot_sha256": "b" * 64,
    }
    result = accelerator_client._process(args, tmp_path / "source", tmp_path / "template", job)
    assert result["status"] == "succeeded"
    assert len(calls) == 1
    assert calls[0]["layout_snapshot_sha256"] == "b" * 64


def test_template_feature_cache_keeps_multiple_pages_and_evicts_lru(monkeypatch, tmp_path):
    processor = CVCudaBatchProcessor(batch_size=2)
    decoded = np.zeros((100, 200), dtype=np.uint8)
    extracted = []
    monkeypatch.setattr(processor, "_decode", lambda _path: decoded)

    import worker.cvcuda_batch_processor as module

    def fake_extract(images, **_kwargs):
        value = f"features-{len(extracted)}"
        extracted.append(value)
        return [((100, 2), value)]

    monkeypatch.setattr(module, "_extract_batch", fake_extract)
    hashes = [f"{index:064x}" for index in range(MAX_TEMPLATE_CACHE_ENTRIES + 1)]
    for index, digest in enumerate(hashes[:-1]):
        processor._template_features(tmp_path / f"template-{index}.png", digest)

    processor._template_features(tmp_path / "template-0.png", hashes[0])
    processor._template_features(tmp_path / "template-new.png", hashes[-1])

    assert len(processor._template_cache) == MAX_TEMPLATE_CACHE_ENTRIES
    assert hashes[0] in processor._template_cache
    assert hashes[1] not in processor._template_cache
    assert len(extracted) == MAX_TEMPLATE_CACHE_ENTRIES + 1
