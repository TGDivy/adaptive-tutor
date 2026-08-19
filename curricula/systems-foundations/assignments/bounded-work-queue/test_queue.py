import pytest
from src.bounded_queue import BoundedQueue


def test_fifo_and_capacity() -> None:
    queue = BoundedQueue(2)
    assert queue.put("a")
    assert queue.put("b")
    assert not queue.put("c")
    assert queue.get() == "a"
    assert queue.get() == "b"
    assert queue.get() is None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BoundedQueue(0)
