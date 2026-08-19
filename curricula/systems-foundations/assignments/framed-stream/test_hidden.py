from src.framing import FrameDecoder


def frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(2, "big") + payload


def test_every_header_boundary_and_coalesced_frames() -> None:
    payloads = [b"", b"a", b"payload"]
    encoded = b"".join(frame(item) for item in payloads)
    for split in range(len(encoded) + 1):
        decoder = FrameDecoder()
        observed = decoder.feed(encoded[:split])
        observed.extend(decoder.feed(encoded[split:]))
        assert observed == payloads
