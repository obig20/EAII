"""
tests/channels/test_rate_limiter.py

Tests for in-memory rate limiter protecting public unauthenticated endpoints.
"""

import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.channels.web.rate_limit import InMemoryRateLimiter


class TestInMemoryRateLimiter:
    def test_requests_within_limit_pass(self):
        limiter = InMemoryRateLimiter(requests_per_minute=5)
        req = MagicMock()
        req.headers = {}
        req.client.host = "192.168.1.100"

        for _ in range(5):
            limiter(req)  # Should not raise

    def test_exceeding_limit_raises_http_429(self):
        limiter = InMemoryRateLimiter(requests_per_minute=3)
        req = MagicMock()
        req.headers = {}
        req.client.host = "192.168.1.101"

        for _ in range(3):
            limiter(req)

        with pytest.raises(HTTPException) as exc_info:
            limiter(req)
        assert exc_info.value.status_code == 429
        assert "Too many confirmation requests" in exc_info.value.detail

    def test_rate_limiter_isolates_different_ips(self):
        limiter = InMemoryRateLimiter(requests_per_minute=2)

        req1 = MagicMock()
        req1.headers = {}
        req1.client.host = "10.0.0.1"

        req2 = MagicMock()
        req2.headers = {}
        req2.client.host = "10.0.0.2"

        limiter(req1)
        limiter(req1)

        # IP 1 is at limit
        with pytest.raises(HTTPException):
            limiter(req1)

        # IP 2 should still be allowed
        limiter(req2)
        limiter(req2)
