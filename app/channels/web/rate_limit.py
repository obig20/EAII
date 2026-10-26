"""
app/channels/web/rate_limit.py

In-memory sliding window rate limiter for public unauthenticated endpoints
(such as the retailer balance confirmation and dispute pages).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request, status


class InMemoryRateLimiter:
    """
    Sliding window rate limiter based on client IP.
    Thread-safe and requires no external dependency (Redis, etc.).
    """

    def __init__(self, requests_per_minute: int = 15):
        self.requests_per_minute = requests_per_minute
        self.window_seconds = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def get_client_ip(self, request: Request) -> str:
        # Check standard reverse proxy headers first
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def __call__(self, request: Request) -> None:
        client_ip = self.get_client_ip(request)
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            timestamps = self._requests[client_ip]
            # Prune older than 1 minute
            fresh_timestamps = [ts for ts in timestamps if ts > cutoff]

            if len(fresh_timestamps) >= self.requests_per_minute:
                self._requests[client_ip] = fresh_timestamps
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many confirmation requests from this address. Please wait a minute and try again.",
                )

            fresh_timestamps.append(now)
            self._requests[client_ip] = fresh_timestamps


# Default rate limiter: 15 requests per minute for public token endpoints
confirmation_rate_limiter = InMemoryRateLimiter(requests_per_minute=15)
