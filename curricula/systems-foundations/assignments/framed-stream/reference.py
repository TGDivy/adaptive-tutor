class FrameDecoder:
    def __init__(self, max_frame_size: int = 65535) -> None:
        if max_frame_size < 0:
            raise ValueError("max_frame_size cannot be negative")
        self._max_frame_size = max_frame_size
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while len(self._buffer) >= 2:
            length = int.from_bytes(self._buffer[:2], "big")
            if length > self._max_frame_size:
                raise ValueError("declared frame exceeds maximum")
            if len(self._buffer) < 2 + length:
                break
            frames.append(bytes(self._buffer[2 : 2 + length]))
            del self._buffer[: 2 + length]
        return frames
