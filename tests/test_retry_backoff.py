import pytest

from services.retry_backoff import (
    exponential_retry_delay,
    parse_retry_after_seconds,
)


def test_retry_after_supports_delta_seconds_and_http_date():
    assert parse_retry_after_seconds("12.5") == 12.5
    assert parse_retry_after_seconds(
        "Wed, 21 Oct 2015 07:28:00 GMT",
        now_epoch=1_445_412_470.0,
    ) == 10.0
    assert parse_retry_after_seconds("Wed, 21 Oct 2015 07:00:00 GMT") is None
    assert parse_retry_after_seconds("not-a-date") is None


def test_exponential_retry_delay_has_jitter_and_a_hard_ceiling(monkeypatch):
    monkeypatch.setattr(
        "services.retry_backoff.random.uniform",
        lambda _low, high: high,
    )

    assert exponential_retry_delay(
        1, base_delay_s=1.0, ceiling_s=5.0
    ) == pytest.approx(1.25)
    assert exponential_retry_delay(
        2, base_delay_s=1.0, ceiling_s=5.0
    ) == pytest.approx(2.5)
    assert exponential_retry_delay(
        1,
        base_delay_s=1.0,
        retry_after_s=4.0,
        ceiling_s=5.0,
    ) == pytest.approx(5.0)
    assert exponential_retry_delay(
        1,
        base_delay_s=1.0,
        retry_after_s=3_600.0,
        ceiling_s=5.0,
    ) == pytest.approx(3_600.0)


@pytest.mark.parametrize("attempt", [0, -1])
def test_exponential_retry_delay_rejects_invalid_attempt(attempt):
    with pytest.raises(ValueError, match="attempt"):
        exponential_retry_delay(attempt, base_delay_s=1.0)
