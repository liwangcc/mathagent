"""Evidence reader contract (answer-sheet B3).

``read_evidence_file`` is the only path from a teacher request to evidence
bytes: files listed in the record only, inside the batch directory only,
and only while the bytes still match the recorded hash.
"""

import numpy as np
import pytest

from services.answer_sheet_column_transcription import (
    EVIDENCE_DIRNAME,
    TranscriptionEvidence,
    read_evidence_file,
)

_ATTEMPT_DIR = ("paper1", "page1", "attempt1")


def _record(tmp_path) -> dict:
    evidence = TranscriptionEvidence(tmp_path, "paper1", "page1", attempt_id="attempt1")
    evidence.write_text("response-primary.txt", "hello", "model_response")
    evidence.write_json("request-primary.json", {"entries": []}, "request")
    evidence.write_image("column-01.png", np.full((4, 4, 3), 200, dtype=np.uint8), "column")
    return {"evidence": evidence.manifest()}


def _attempt_root(tmp_path):
    return tmp_path.joinpath(EVIDENCE_DIRNAME, *_ATTEMPT_DIR)


def test_read_evidence_file_returns_recorded_bytes(tmp_path):
    record = _record(tmp_path)
    assert read_evidence_file(tmp_path, record, "response-primary.txt") == b"hello"
    png = read_evidence_file(tmp_path, record, "column-01.png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_read_evidence_file_rejects_unlisted_name(tmp_path):
    record = _record(tmp_path)
    (_attempt_root(tmp_path) / "column-02.png").write_bytes(b"not listed")
    with pytest.raises(ValueError):
        read_evidence_file(tmp_path, record, "column-02.png")


def test_read_evidence_file_rejects_traversal_names(tmp_path):
    record = _record(tmp_path)
    for name in ("../record.json", "..%2Frecord.json", "sub/record.json", ""):
        with pytest.raises(ValueError):
            read_evidence_file(tmp_path, record, name)


def test_read_evidence_file_rejects_traversal_directory(tmp_path):
    record = _record(tmp_path)
    bad = dict(record["evidence"], directory=f"{EVIDENCE_DIRNAME}/../page1/attempt1")
    with pytest.raises(ValueError):
        read_evidence_file(tmp_path, {"evidence": bad}, "response-primary.txt")


def test_read_evidence_file_detects_tampering(tmp_path):
    record = _record(tmp_path)
    (_attempt_root(tmp_path) / "response-primary.txt").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        read_evidence_file(tmp_path, record, "response-primary.txt")


def test_read_evidence_file_missing_bytes_fails_closed(tmp_path):
    record = _record(tmp_path)
    (_attempt_root(tmp_path) / "column-01.png").unlink()
    with pytest.raises(ValueError):
        read_evidence_file(tmp_path, record, "column-01.png")


def test_evidence_writer_is_write_once(tmp_path):
    evidence = TranscriptionEvidence(tmp_path, "paper1", "page1", attempt_id="attempt1")
    evidence.write_text("response-primary.txt", "first", "model_response")
    with pytest.raises(FileExistsError):
        evidence.write_text("response-primary.txt", "second", "model_response")
