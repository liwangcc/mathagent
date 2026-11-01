from pathlib import Path

import pytest

from services import answer_sheet_session as module


def test_transient_sharing_lock_retries_the_same_complete_snapshot(tmp_path, monkeypatch):
    session = module.AnswerSheetSession("a" * 16, str(tmp_path))
    session.save()
    previous = Path(session.meta_path).read_bytes()
    original_replace = module.os.replace
    calls, sleeps = [], []
    def replace(source, destination):
        calls.append(source)
        assert Path(destination).read_bytes() == previous
        assert Path(source).is_file()
        if len(calls) < 3:
            raise PermissionError("simulated sharing violation")
        original_replace(source, destination)
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    session.status = "source_recognized"
    session.save()
    assert len(calls) == 3 and len(set(calls)) == 1
    assert sleeps == [0.01, 0.02]
    assert module.AnswerSheetSession.load(session.session_id, str(tmp_path)).status == "source_recognized"
    assert not list(Path(session.session_dir).glob("*.tmp"))


@pytest.mark.parametrize("failure,attempts", [(PermissionError, 5), (OSError, 1)])
def test_persistent_failure_is_not_hidden_or_allowed_to_destroy_prior_save(tmp_path, monkeypatch, failure, attempts):
    session = module.AnswerSheetSession("b" * 16, str(tmp_path))
    session.save()
    previous = Path(session.meta_path).read_bytes()
    calls = []
    def replace(source, destination):
        calls.append(source)
        raise failure("simulated permanent storage failure")
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", lambda delay: None)
    session.status = "failed"
    with pytest.raises(failure):
        session.save()
    assert len(calls) == attempts
    assert Path(session.meta_path).read_bytes() == previous
    assert not list(Path(session.session_dir).glob("*.tmp"))
