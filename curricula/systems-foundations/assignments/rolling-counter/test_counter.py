import pytest
from src.rolling_counter import RollingCounter


def test_counts_recent_events() -> None:
    counter = RollingCounter(10)
    counter.record(0)
    counter.record(8)
    assert counter.count(10) == 1
    assert counter.count(10) == 1


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RollingCounter(0)
