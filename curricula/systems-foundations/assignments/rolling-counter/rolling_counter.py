class RollingCounter:
    def __init__(self, window: float) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self._window = window
        self._timestamps: list[float] = []

    def record(self, timestamp: float) -> None:
        self._timestamps.append(timestamp)

    def count(self, now: float) -> int:
        for timestamp in self._timestamps:
            if now - timestamp >= self._window:
                self._timestamps.remove(timestamp)
        return len(self._timestamps)
