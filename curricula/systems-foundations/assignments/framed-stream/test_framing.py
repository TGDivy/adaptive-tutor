import pytest
from src.framing import FrameDecoder


def frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(2, "big") + payload


def test_split_payload() -> None:
    decoder = FrameDecoder()
    encoded = frame(b"hello")
    assert decoder.feed(encoded[:4]) == []
    assert decoder.feed(encoded[4:]) == [b"hello"]


def test_rejects_oversized_frame() -> None:
    decoder = FrameDecoder(max_frame_size=3)
    with pytest.raises(ValueError):
        decoder.feed((4).to_bytes(2, "big"))
