from types import SimpleNamespace

from services.scoring_compat import display_metric, exam_points, record_correct_rate


def test_legacy_normalized_daily_score_remains_readable():
    row = SimpleNamespace(
        category="daily", score=0.8, max_score=None,
        correct_rate=None, scoring_version=1,
    )
    assert record_correct_rate(row) == 0.8
    assert display_metric(row) == {"kind": "correct_rate", "value": 0.8, "maximum": None}


def test_legacy_exam_normalized_score_uses_100_point_fallback():
    row = {
        "category": "exam", "score": 0.85, "max_score": None,
        "correct_rate": None, "scoring_version": 1,
    }
    assert exam_points(row) == (85.0, 100.0)
    assert record_correct_rate(row) == 0.85


def test_v2_exam_uses_explicit_points_and_maximum():
    row = {
        "category": "exam", "score": 12.5, "max_score": 20.0,
        "correct_rate": 0.5, "scoring_version": 2,
    }
    assert exam_points(row) == (12.5, 20.0)
    assert record_correct_rate(row) == 0.5
    assert display_metric(row) == {"kind": "points", "value": 12.5, "maximum": 20.0}


def test_v2_daily_never_reinterprets_numeric_score_as_correct_rate():
    malformed = {
        "category": "daily", "score": 80.0, "max_score": None,
        "correct_rate": None, "scoring_version": 2,
    }
    assert record_correct_rate(malformed) is None
