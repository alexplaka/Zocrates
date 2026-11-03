"""
Crossref DOI metadata validator and resolver.
"""

import logging
from typing import Any

import httpx

from zocrates.discovery.providers.base import DEFAULT_POLITE_EMAIL, make_user_agent
from zocrates.rag.zotero_db import normalize_doi

logger = logging.getLogger(__name__)


class CrossrefValidator:
    """Validates DOI metadata and queries bibliographic records via Crossref REST API."""

    def __init__(self, email: str = DEFAULT_POLITE_EMAIL, timeout: float = 5.0) -> None:
        """Initialize the Crossref validator.

        Args:
            email: Contact email used for polite-pool API identification.
            timeout: HTTP request timeout in seconds.
        """
        self.email = email
        self.timeout = timeout

    async def validate_doi(self, doi: str, email: str | None = None) -> dict[str, Any] | None:
        """
        Validate and retrieve Crossref work metadata for a given DOI.

        Args:
            doi: Digital Object Identifier.
            email: User or developer email for polite pool identification.

        Returns:
            Dictionary containing Crossref 'message' payload if valid, or None.
        """
        clean_doi = normalize_doi(doi)
        if not clean_doi:
            return None

        polite_email = email or self.email
        url = f"https://api.crossref.org/works/{clean_doi}"
        headers = {
            "User-Agent": make_user_agent(polite_email),
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.debug("Crossref returned status %d for DOI %s", resp.status_code, clean_doi)
                    return None
                data: dict[str, Any] = resp.json()
                message = data.get("message")
                return message if isinstance(message, dict) else None
        except Exception as e:
            logger.debug("Crossref validation failed for DOI %s: %s", clean_doi, e)
            return None
