class RollingCounter:
    def __init__(self, window: float) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self._window = window
        self._timestamps: list[float] = []

    def record(self, timestamp: float) -> None:
        self._timestamps.append(timestamp)

    def count(self, now: float) -> int:
        expired = 0
        while (
            expired < len(self._timestamps)
            and now - self._timestamps[expired] >= self._window
        ):
            expired += 1
        del self._timestamps[:expired]
        return len(self._timestamps)
