class BoundedQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items = [None] * capacity
        self._read = 0
        self._write = 0

    @property
    def capacity(self) -> int:
        return len(self._items)

    def put(self, value: object) -> bool:
        if self._write == self._read and self._items[self._write] is not None:
            return False
        self._items[self._write] = value
        self._write = (self._write + 1) % self.capacity
        return True

    def get(self) -> object | None:
        if self._read == self._write:
            return None
        value = self._items[self._read]
        self._items[self._read] = None
        self._read = (self._read + 1) % self.capacity
        return value
