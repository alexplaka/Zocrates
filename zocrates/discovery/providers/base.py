"""
Base interfaces and utilities for scholarly search providers.
"""

import asyncio
import time
from abc import ABC, abstractmethod

from zocrates.discovery.models import CandidatePaper

DEFAULT_POLITE_EMAIL: str = "zocrates-dev@proton.me"
DEFAULT_USER_AGENT: str = f"Zocrates/0.2.0 (mailto:{DEFAULT_POLITE_EMAIL})"


def make_user_agent(email: str = DEFAULT_POLITE_EMAIL) -> str:
    """Build a respectful User-Agent header string adhering to API etiquette."""
    polite_email = email.strip() if email and email.strip() else DEFAULT_POLITE_EMAIL
    return f"Zocrates/0.2.0 (mailto:{polite_email})"


class TokenBucketRateLimiter:
    """
    Token-bucket rate limiter for respectful, throttled API consumption.
    Thread/asyncio safe.
    """

    def __init__(self, rate: float = 10.0, capacity: float = 10.0) -> None:
        """
        Args:
            rate: Refill rate in tokens per second.
            capacity: Maximum burst capacity in tokens.
        """
        self.rate = max(0.1, float(rate))
        self.capacity = max(1.0, float(capacity))
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquire `tokens` from the bucket, sleeping if necessary until replenished."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens < tokens:
                needed = tokens - self.tokens
                wait_time = needed / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
                self.last_update = time.monotonic()
            else:
                self.tokens -= tokens


class SearchProvider(ABC):
    """Abstract base class for scholarly literature search providers."""

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        is_oa_only: bool = False,
    ) -> list[CandidatePaper]:
        """
        Search for scholarly literature matching the query and filters.

        Args:
            query: Free-text search terms or conceptual query.
            limit: Maximum number of candidate papers to return.
            year_from: Optional minimum publication year (inclusive).
            year_to: Optional maximum publication year (inclusive).
            is_oa_only: When True, restrict results to open-access works.

        Returns:
            List of CandidatePaper instances.
        """
        ...

    @abstractmethod
    async def resolve_identifier(self, identifier: str) -> CandidatePaper | None:
        """
        Resolve a single scholarly paper by DOI, arXiv ID, or provider-specific ID.

        Args:
            identifier: DOI, arXiv ID, or provider ID string.

        Returns:
            CandidatePaper if resolved, or None if not found.
        """
        ...
