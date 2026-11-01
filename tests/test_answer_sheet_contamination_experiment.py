"""Offline checks of the experiment harness, not claimed model evidence."""
import asyncio
import json

import httpx
import numpy as np
import pytest
from PIL import Image

from scripts import run_answer_sheet_contamination_experiment as experiment
from services.answer_sheet_bubble_recognizer import recognize_bubble_page
from services.answer_sheet_column_transcription import build_transcription_request, masked_page


def test_synthetic_samples_cover_required_states_and_keep_truth_out_of_requests(tmp_path):
    samples = experiment.make_samples(tmp_path)
    assert len(samples) <= 5
    assert {case["case"] for sample in samples for case in sample["cases"]} >= {
        "correct_equivalent", "wrong_fraction", "blank_text", "multiple_marks", "long_wrong_propagated",
    }
    for sample in samples:
        request = build_transcription_request(sample["plan"])
        assert "standard_answer" not in request["prompt"]
        assert "25又4/5" not in request["prompt"]
        assert "3/4" not in request["prompt"]
        combined = experiment.combined_prompt(request["prompt"], sample)
        assert combined.startswith(request["prompt"])
        assert "judgments" in combined and "教师答案合同" in combined
        assert "long_wrong_propagated" not in combined
    sample = samples[1]
    image = np.asarray(Image.open(tmp_path / sample["image"]))
    rows = sample["bubble_rows"]
    results = recognize_bubble_page(image, list(rows.values()), {no: ["C"] for no in rows})
    assert [results[no]["outcome"] for no in ("2", "3", "4", "5")] == ["multiple", "blank", "correct", "wrong"]
    masked = masked_page(image, sample["plan"])
    for row in rows.values():
        for cell in row["cells"]:
            x1, y1, x2, y2 = cell["pixel_box"]
            assert np.all(masked[y1:y2, x1:x2] == 255)


def test_combined_extraction_preserves_wrong_text_and_rejects_missing_judgments():
    value = {"entries": [{"entry_id": "c1-1", "student_answer": "3/4", "answer_state": "visible", "confidence": .99}],
             "judgments": [{"entry_id": "c1-1", "result": "wrong", "explanation": "与标准答案不同"}]}
    request = {"entry_ids": ["c1-1"]}
    text, judgments = experiment.extract_combined(json.dumps(value), request)
    assert json.loads(text)["entries"][0]["student_answer"] == "3/4"
    assert judgments == value["judgments"]
    value["judgments"] = []
    with pytest.raises(ValueError, match="identity"):
        experiment.extract_combined(json.dumps(value), request)


def test_http_budget_counts_failed_attempts_and_stops_before_21st(monkeypatch, tmp_path):
    sent = []

    async def original(_client, request, **_kwargs):
        sent.append(request)
        return httpx.Response(503, content=b'{"error":"synthetic failure"}', request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", original)
    archive = experiment.WireArchive(tmp_path)

    async def run():
        with archive.install():
            async with httpx.AsyncClient() as client:
                for _ in range(20):
                    await client.post("https://synthetic.invalid/chat/completions", json={"model": "fixture", "messages": []},
                                      headers={"Authorization": "Bearer secret-canary"})
                with pytest.raises(experiment.BudgetExceeded):
                    await client.post("https://synthetic.invalid/chat/completions", json={"model": "fixture", "messages": []})

    asyncio.run(run())
    assert len(sent) == len(archive.calls) == 20
    assert all(item["status"] == 503 for item in archive.calls)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"secret-canary" not in path.read_bytes()
    assert (tmp_path / "wire/01-response.bin").read_bytes() == b'{"error":"synthetic failure"}'


def test_evidence_manifest_rejects_changed_raw_response(tmp_path):
    experiment.save(tmp_path / "sources.json", {})
    response = tmp_path / "raw.txt"
    response.write_text("actual archived reply", encoding="utf-8")
    experiment.manifest(tmp_path, "complete")
    experiment.verify_manifest(tmp_path)
    response.write_text("modified reply", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        experiment.verify_manifest(tmp_path)


def test_comparison_uses_production_awarded_score_even_when_verdict_matches():
    sample = {"id": "example", "cases": [{"no": "1", "case": "partial", "text": "1", "expected": "partial"}]}
    results = {f"example-{arm}": {
        "answers": [{"question_no": "1", "answer": "1", "status": "recognized", "reason": "synthetic"}],
        "grading": {"details": [{"question_no": "1", "result": "partial", "awarded_score": score}]},
    } for arm, score in (("A", 1.0), ("B", 2.0))}
    row = experiment.compare([sample], results)[0]
    assert row["grade_differs"] and not row["text_differs"]
    assert (row["A"]["score"], row["B"]["score"]) == (1.0, 2.0)


def test_offline_replay_cannot_make_http_requests(monkeypatch, tmp_path):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("network called in replay")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    archive = experiment.WireArchive(tmp_path, allow_network=False)

    async def run():
        with archive.install():
            async with httpx.AsyncClient() as client:
                with pytest.raises(experiment.BudgetExceeded, match="offline"):
                    await client.post("https://synthetic.invalid", json={"model": "fixture", "messages": []})
    asyncio.run(run())
    assert archive.calls == []


def test_replay_allows_only_rubric_key_order_changes():
    one = {"prompt": 'verbatim\n教师答案合同=[{"x":1,"y":2}]', "max_tokens": 10}
    two = {"prompt": 'verbatim\n教师答案合同=[{"y":2,"x":1}]', "max_tokens": 10}
    assert experiment.request_identity(one) == experiment.request_identity(two)
    two["prompt"] = two["prompt"].replace('"x":1', '"x":3')
    assert experiment.request_identity(one) != experiment.request_identity(two)
