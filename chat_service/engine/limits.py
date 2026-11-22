"""Rate and concurrency limits.

* In-memory versions: one instance only (development, tests).
* Redis versions (Phase 12): shared across instances. Each check is a single atomic Lua script, so two
  servers can't both let the 21st request through. Concurrency slots are leases with an expiry, so a crashed
  instance can't hold an org's slots forever.
* Run this Redis **without persistence** (``--save "" --appendonly no``): limit state is short-lived, and
  restoring an old snapshot would bring back stale slots (found in the Phase 12 failure drill).
* If Redis is unreachable, the limiters **fail open** (log + allow). They protect capacity, not money: the
  wallet's own atomic checks still prevent overspending.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Protocol

from chat_service.metrics import LIMITER_FAIL_OPEN

log = logging.getLogger("chat_service.limits")


class RateLimiter(Protocol):
    async def allow(self, key: uuid.UUID) -> bool: ...


class ConcurrencyLimiter(Protocol):
    async def try_acquire(self, key: uuid.UUID) -> str | None: ...

    async def release(self, key: uuid.UUID, slot: str | None) -> None: ...


class MemoryRateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._hits: dict[uuid.UUID, deque[float]] = defaultdict(deque)

    async def allow(self, key: uuid.UUID) -> bool:
        now = self.clock()
        q = self._hits[key]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


class MemoryConcurrencyLimiter:
    def __init__(self, max_per_key: int) -> None:
        self.max_per_key = max_per_key
        self._active: dict[uuid.UUID, int] = defaultdict(int)

    async def try_acquire(self, key: uuid.UUID) -> str | None:
        if self._active[key] >= self.max_per_key:
            return None
        self._active[key] += 1
        return "local"

    async def release(self, key: uuid.UUID, slot: str | None) -> None:
        if slot is not None:
            self._active[key] = max(0, self._active[key] - 1)


_RATE_LUA = """
local key, now, window, limit, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return 1
"""

_SLOT_LUA = """
local key, now, lease, limit, slot = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now + lease, slot)
redis.call('PEXPIRE', key, lease)
return 1
"""


class RedisRateLimiter:
    def __init__(self, redis, per_minute: int, prefix: str = "aos:rate:") -> None:
        self.redis = redis
        self.per_minute = per_minute
        self.prefix = prefix
        self._script = redis.register_script(_RATE_LUA)

    async def allow(self, key: uuid.UUID) -> bool:
        now_ms = int(time.time() * 1000)
        try:
            return bool(
                await self._script(
                    keys=[f"{self.prefix}{key}"], args=[now_ms, 60_000, self.per_minute, uuid.uuid4().hex]
                )
            )
        except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
            log.warning("rate limiter unavailable, allowing request: %s", type(exc).__name__)
            LIMITER_FAIL_OPEN.labels("rate").inc()
            return True


class RedisConcurrencyLimiter:
    def __init__(self, redis, max_per_key: int, lease_seconds: int = 300, prefix: str = "aos:streams:") -> None:
        self.redis = redis
        self.max_per_key = max_per_key
        self.lease_ms = lease_seconds * 1000
        self.prefix = prefix
        self._script = redis.register_script(_SLOT_LUA)

    async def try_acquire(self, key: uuid.UUID) -> str | None:
        slot = uuid.uuid4().hex
        try:
            ok = await self._script(
                keys=[f"{self.prefix}{key}"], args=[int(time.time() * 1000), self.lease_ms, self.max_per_key, slot]
            )
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("concurrency limiter unavailable, allowing request: %s", type(exc).__name__)
            LIMITER_FAIL_OPEN.labels("concurrency").inc()
            return "unlimited"
        return slot if ok else None

    async def release(self, key: uuid.UUID, slot: str | None) -> None:
        if not slot or slot == "unlimited":
            return
        try:
            await self.redis.zrem(f"{self.prefix}{key}", slot)
        except Exception as exc:  # noqa: BLE001 - the lease expires on its own
            log.warning("could not release concurrency slot: %s", type(exc).__name__)


def build_limiters(settings) -> tuple[RateLimiter, ConcurrencyLimiter]:
    if settings.redis_url:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
        return (
            RedisRateLimiter(client, settings.rate_limit_per_user_per_min),
            RedisConcurrencyLimiter(
                client, settings.max_concurrent_streams_per_org, settings.stream_slot_lease_seconds
            ),
        )
    return (
        MemoryRateLimiter(settings.rate_limit_per_user_per_min),
        MemoryConcurrencyLimiter(settings.max_concurrent_streams_per_org),
    )
