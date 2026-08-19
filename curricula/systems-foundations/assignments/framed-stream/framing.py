class FrameDecoder:
    def __init__(self, max_frame_size: int = 65535) -> None:
        self._max_frame_size = max_frame_size
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        if len(self._buffer) < 2:
            return []
        length = int.from_bytes(self._buffer[:2], "big")
        if len(self._buffer) < 2 + length:
            return []
        payload = bytes(self._buffer[2 : 2 + length])
        del self._buffer[: 2 + length]
        return [payload]
