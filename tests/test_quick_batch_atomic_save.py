from pathlib import Path

import pytest

from services import quick_grader as module


def test_transient_sharing_lock_preserves_then_atomically_replaces_batch(tmp_path, monkeypatch):
    batch = module.QuickBatch("save-retry", str(tmp_path))
    batch.save_meta()
    previous = Path(batch.meta_path).read_bytes()
    original_replace = module.os.replace
    calls, sleeps = [], []

    def replace(source, destination):
        calls.append(source)
        assert Path(destination).read_bytes() == previous
        if len(calls) < 3:
            raise PermissionError("simulated sharing violation")
        original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    batch.status = "done"
    batch.save_meta()
    assert len(calls) == 3 and len(set(calls)) == 1
    assert sleeps == [0.01, 0.02]
    assert module.QuickBatch.load_meta(batch.batch_id, str(tmp_path)).status == "done"
    assert not list(Path(batch.batch_dir).glob("*.tmp"))


@pytest.mark.parametrize("failure,attempts", [(PermissionError, 5), (OSError, 1)])
def test_failed_batch_write_keeps_prior_snapshot_and_reports_error(tmp_path, monkeypatch, failure, attempts):
    batch = module.QuickBatch("save-failure", str(tmp_path))
    batch.save_meta()
    previous = Path(batch.meta_path).read_bytes()
    calls = []

    def replace(source, destination):
        calls.append(source)
        raise failure("simulated storage failure")

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", lambda delay: None)
    batch.status = "done"
    with pytest.raises(failure):
        batch.save_meta()
    assert len(calls) == attempts
    assert Path(batch.meta_path).read_bytes() == previous
    assert not list(Path(batch.batch_dir).glob("*.tmp"))
