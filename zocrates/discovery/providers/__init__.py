"""
Scholarly literature discovery providers and resolvers.
"""

from zocrates.discovery.providers.base import (
    DEFAULT_POLITE_EMAIL,
    DEFAULT_USER_AGENT,
    SearchProvider,
    TokenBucketRateLimiter,
    make_user_agent,
)
from zocrates.discovery.providers.crossref import CrossrefValidator
from zocrates.discovery.providers.openalex import OpenAlexProvider
from zocrates.discovery.providers.unpaywall import UnpaywallResolver

__all__ = [
    "DEFAULT_POLITE_EMAIL",
    "DEFAULT_USER_AGENT",
    "SearchProvider",
    "TokenBucketRateLimiter",
    "make_user_agent",
    "OpenAlexProvider",
    "UnpaywallResolver",
    "CrossrefValidator",
]
