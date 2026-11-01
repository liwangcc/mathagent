from __future__ import annotations

import threading

import numpy as np
import pytest
from PIL import Image

from mathagent_worker import preprocessing
from mathagent_worker.vksift_resident import ResidentGpuError, ResidentVulkanSift


class _Sink:
    def write(self, payload):
        return len(payload)

    def flush(self):
        return None

    def close(self):
        return None


class _BlockingSource:
    def __init__(self, released: threading.Event):
        self.released = released

    def read(self, _size):
        self.released.wait(2.0)
        return b""

    def close(self):
        self.released.set()


class _BlockingProcess:
    def __init__(self):
        self.released = threading.Event()
        self.stdin = _Sink()
        self.stdout = _BlockingSource(self.released)
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.released.set()

    def wait(self, timeout=None):
        self.released.wait(timeout)
        return self.returncode


def test_resident_runtime_hash_mismatch_fails_closed(tmp_path, monkeypatch):
    helper = tmp_path / "vksift_resident.exe"
    library = tmp_path / "vulkansift.dll"
    runtime = tmp_path / "vcruntime140.dll"
    helper.write_bytes(b"wrong helper")
    library.write_bytes(b"wrong library")
    runtime.write_bytes(b"wrong runtime")
    resident = ResidentVulkanSift()
    monkeypatch.setattr(resident, "_paths", lambda: (helper, library, runtime))

    with pytest.raises(ResidentGpuError, match="runtime_mismatch"):
        resident._start()


def test_resident_timeout_kills_the_native_process(monkeypatch):
    resident = ResidentVulkanSift()
    process = _BlockingProcess()
    resident._process = process
    monkeypatch.setattr(resident, "_start", lambda: process)
    gray = np.zeros((32, 32), dtype=np.uint8)

    with pytest.raises(ResidentGpuError, match="gpu_resident_timeout"):
        resident.extract(gray, timeout_seconds=0.1)

    assert process.killed is True
    assert resident._process is None


def test_preprocessing_resident_failure_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (64, 48), "white").save(source)

    import mathagent_worker.vksift_resident as resident_module

    monkeypatch.setattr(
        resident_module,
        "extract_resident",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ResidentGpuError("gpu_resident_native_error")
        ),
    )

    with pytest.raises(ResidentGpuError, match="gpu_resident_native_error"):
        preprocessing._extract_gpu(source, tmp_path)
