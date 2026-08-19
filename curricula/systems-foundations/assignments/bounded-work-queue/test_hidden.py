from src.bounded_queue import BoundedQueue


def test_wraparound_and_repeated_cycles() -> None:
    queue = BoundedQueue(3)
    for cycle in range(30):
        expected = []
        for offset in range(3):
            value = (cycle, offset)
            assert queue.put(value)
            expected.append(value)
        assert not queue.put("overflow")
        for value in expected:
            assert queue.get() == value
        assert queue.get() is None
