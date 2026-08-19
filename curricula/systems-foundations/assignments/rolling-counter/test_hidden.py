from src.rolling_counter import RollingCounter


def test_expires_prefix_and_respects_boundary() -> None:
    counter = RollingCounter(5)
    for timestamp in (1, 2, 3, 6, 7):
        counter.record(timestamp)
    assert counter.count(7) == 3
    assert counter.count(8) == 2
    assert counter.count(8) == 2
