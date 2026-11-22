"""Per-process rate and concurrency limits.

These work for a single instance. With several instances behind a load balancer, move both to Redis
(same interface) so limits are shared. Tracked in docs/contracts/CONTRACTS.md (open items).
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._hits: dict[uuid.UUID, deque[float]] = defaultdict(deque)

    def allow(self, key: uuid.UUID) -> bool:
        now = self.clock()
        q = self._hits[key]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


class ConcurrencyLimiter:
    def __init__(self, max_per_key: int) -> None:
        self.max_per_key = max_per_key
        self._active: dict[uuid.UUID, int] = defaultdict(int)

    def try_acquire(self, key: uuid.UUID) -> bool:
        if self._active[key] >= self.max_per_key:
            return False
        self._active[key] += 1
        return True

    def release(self, key: uuid.UUID) -> None:
        self._active[key] = max(0, self._active[key] - 1)
