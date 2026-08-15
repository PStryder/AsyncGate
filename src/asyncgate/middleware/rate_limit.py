"""Rate limiting middleware for AsyncGate API."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from asyncgate.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RateLimitRule:
    """Rate limit configuration for a specific scope."""

    calls: int  # Number of calls allowed
    window_seconds: int  # Time window in seconds
    key_prefix: str = ""  # Optional prefix for rate limit key


class RateLimiterBackend(ABC):
    """Abstract base class for rate limiter backends."""

    @abstractmethod
    async def check_rate_limit(
        self, key: str, max_calls: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """
        Check if request should be rate limited.

        Args:
            key: Rate limit key (typically client ID or IP)
            max_calls: Maximum calls allowed in window
            window_seconds: Window size in seconds

        Returns:
            Tuple of (allowed: bool, remaining: int, reset_time: int)
            - allowed: Whether request should proceed
            - remaining: Calls remaining in current window
            - reset_time: Unix timestamp when window resets
        """
        pass

    @abstractmethod
    async def reset(self, key: str):
        """Reset rate limit for a specific key."""
        pass


class InMemoryRateLimiter(RateLimiterBackend):
    """
    In-memory rate limiter using sliding window.

    Good for development and single-instance deployments.
    Not suitable for multi-instance production (no shared state).
    """

    def __init__(self):
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self, key: str, max_calls: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """Check rate limit using sliding window."""
        async with self._lock:
            now = time.time()
            window_start = now - window_seconds

            # Clean old entries
            self._windows[key] = [
                ts for ts in self._windows[key] if ts > window_start
            ]

            # Check limit
            current_calls = len(self._windows[key])
            allowed = current_calls < max_calls
            remaining = max(0, max_calls - current_calls - (1 if allowed else 0))

            # Calculate reset time (when oldest entry expires)
            if self._windows[key]:
                reset_time = int(self._windows[key][0] + window_seconds)
            else:
                reset_time = int(now + window_seconds)

            # Record this call if allowed
            if allowed:
                self._windows[key].append(now)

            return allowed, remaining, reset_time

    async def reset(self, key: str):
        """Reset rate limit for key."""
        async with self._lock:
            if key in self._windows:
                del self._windows[key]


class RedisRateLimiter(RateLimiterBackend):
    """
    Redis-backed rate limiter using sorted sets.

    Suitable for multi-instance production deployments.
    Requires redis to be installed: pip install redis[hiredis]
    """

    def __init__(self, redis_url: str):
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package required for RedisRateLimiter. "
                "Install with: pip install redis[hiredis]"
            )

        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        logger.info(f"Redis rate limiter initialized: {redis_url}")

    async def check_rate_limit(
        self, key: str, max_calls: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """Check rate limit using Redis sorted set."""
        now = time.time()
        window_start = now - window_seconds
        redis_key = f"ratelimit:{key}"

        pipe = self.redis.pipeline()

        # Remove old entries
        pipe.zremrangebyscore(redis_key, 0, window_start)

        # Count current entries
        pipe.zcard(redis_key)

        # Add current request (with score = timestamp)
        pipe.zadd(redis_key, {str(now): now})

        # Set expiry on key
        pipe.expire(redis_key, window_seconds)

        results = await pipe.execute()
        current_calls = results[1]  # Result of zcard

        allowed = current_calls < max_calls
        remaining = max(0, max_calls - current_calls - (1 if allowed else 0))

        # Calculate reset time
        oldest_entries = await self.redis.zrange(redis_key, 0, 0, withscores=True)
        if oldest_entries:
            oldest_time = oldest_entries[0][1]
            reset_time = int(oldest_time + window_seconds)
        else:
            reset_time = int(now + window_seconds)

        # Remove the entry we just added if not allowed
        if not allowed:
            await self.redis.zrem(redis_key, str(now))

        return allowed, remaining, reset_time

    async def reset(self, key: str):
        """Reset rate limit for key."""
        await self.redis.delete(f"ratelimit:{key}")


class RateLimiter:
    """
    Main rate limiter with configurable backend.

    Usage:
        limiter = RateLimiter()
        await limiter.check_request(request)
    """

    def __init__(self, backend: RateLimiterBackend | None = None):
        if backend:
            self.backend = backend
        elif settings.rate_limit_backend == "redis":
            if not settings.redis_url:
                raise ValueError("redis_url required for redis backend")
            self.backend = RedisRateLimiter(settings.redis_url)
        else:
            self.backend = InMemoryRateLimiter()
            logger.info("Using in-memory rate limiter (dev only)")

        self._rules: dict[str, RateLimitRule] = {}
        self._default_rule = RateLimitRule(
            calls=settings.rate_limit_default_calls,
            window_seconds=settings.rate_limit_default_window_seconds,
        )

    def configure_endpoint(
        self, path: str, calls: int, window_seconds: int, key_prefix: str = ""
    ):
        """Configure rate limit for specific endpoint."""
        self._rules[path] = RateLimitRule(
            calls=calls, window_seconds=window_seconds, key_prefix=key_prefix
        )

    async def check_request(
        self, request: Request, key_override: str | None = None
    ) -> None:
        """
        Check if request should be rate limited.

        Args:
            request: FastAPI request
            key_override: Optional custom rate limit key

        Raises:
            HTTPException: 429 if rate limited
        """
        if not settings.rate_limit_active:
            return

        # Determine rule
        path = request.url.path
        rule = self._rules.get(path, self._default_rule)

        # Determine rate limit key
        if key_override:
            key = key_override
        else:
            # P1-4: When auth is enabled, key by API key hash to prevent tenant spoofing.
            # In insecure dev mode, fall back to tenant_id/IP for convenience.
            auth_enabled = not settings.allow_insecure_dev

            if auth_enabled:
                api_key = None
                auth_header = request.headers.get("Authorization")
                if auth_header and auth_header.startswith("Bearer "):
                    api_key = auth_header[7:]
                elif request.headers.get("X-API-Key"):
                    api_key = request.headers.get("X-API-Key")

                if api_key:
                    import hashlib
                    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
                    key = f"{rule.key_prefix}auth:{key_hash}"
                else:
                    # Auth required but missing header; still rate limit by IP.
                    client_ip = request.client.host if request.client else "unknown"
                    key = f"{rule.key_prefix}ip:{client_ip}"
            else:
                # Insecure dev mode: Try tenant_id from query/header, fall back to client IP
                tenant_id = request.query_params.get("tenant_id")
                if not tenant_id:
                    tenant_id = request.headers.get("X-Tenant-ID")

                if tenant_id:
                    key = f"{rule.key_prefix}tenant:{tenant_id}"
                else:
                    # Fall back to client IP
                    client_ip = request.client.host if request.client else "unknown"
                    key = f"{rule.key_prefix}ip:{client_ip}"

        # Check limit
        allowed, remaining, reset_time = await self.backend.check_rate_limit(
            key, rule.calls, rule.window_seconds
        )

        # Set rate limit headers
        request.state.rate_limit_remaining = remaining
        request.state.rate_limit_reset = reset_time

        if not allowed:
            retry_after = reset_time - int(time.time())
            logger.warning(
                f"Rate limit exceeded for {key}: {path} "
                f"(retry after {retry_after}s)"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(rule.calls),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_time),
                },
            )


# Singleton instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get or create rate limiter singleton."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


async def rate_limit_dependency(request: Request) -> None:
    """FastAPI dependency for rate limiting."""
    limiter = get_rate_limiter()
    await limiter.check_request(request)
