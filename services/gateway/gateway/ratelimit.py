"""Per-user token bucket. In-memory => per replica (documented trade-off)."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic):
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self.clock = clock
        self._buckets: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: int) -> tuple[bool, float]:
        """Returns (allowed, seconds_until_next_token)."""
        if self.capacity <= 0:
            return True, 0.0
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1 - tokens) / self.rate


class LoginThrottle:
    """Slows password guessing: N failures per key within a window."""

    def __init__(self, max_failures: int = 5, window: float = 300.0, clock=time.monotonic):
        self.max, self.window, self.clock = max_failures, window, clock
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = recent
            return len(recent) >= self.max

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(self.clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
