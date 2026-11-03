"""
Unpaywall legal Open Access PDF URL resolver.
"""

import logging
from typing import Any

import httpx

from zocrates.discovery.providers.base import DEFAULT_POLITE_EMAIL, make_user_agent
from zocrates.rag.zotero_db import normalize_doi

logger = logging.getLogger(__name__)


class UnpaywallResolver:
    """Resolves open-access fulltext PDF URLs via the Unpaywall API."""

    def __init__(self, email: str = DEFAULT_POLITE_EMAIL, timeout: float = 5.0) -> None:
        """Initialize the Unpaywall resolver.

        Args:
            email: Contact email used for polite-pool API requests.
            timeout: HTTP request timeout in seconds.
        """
        self.email = email
        self.timeout = timeout

    async def resolve_pdf(self, doi: str, email: str | None = None) -> str | None:
        """
        Query Unpaywall for a legal, direct Open Access PDF URL for the given DOI.

        Args:
            doi: Digital Object Identifier (canonical or raw).
            email: User or developer email for polite pool identification.

        Returns:
            Direct PDF URL string if available and verified, or None.
        """
        clean_doi = normalize_doi(doi)
        if not clean_doi:
            return None

        polite_email = email or self.email
        url = f"https://api.unpaywall.org/v2/{clean_doi}"
        params = {"email": polite_email}
        headers = {
            "User-Agent": make_user_agent(polite_email),
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    logger.debug("Unpaywall returned status %d for DOI %s", resp.status_code, clean_doi)
                    return None
                data: dict[str, Any] = resp.json()

            best_oa = data.get("best_oa_location")
            if isinstance(best_oa, dict):
                pdf_url = best_oa.get("url_for_pdf")
                if pdf_url and isinstance(pdf_url, str) and pdf_url.strip():
                    return pdf_url.strip()
                # Fallback to landing URL if it directly points to a PDF
                url_candidate = best_oa.get("url")
                if url_candidate and isinstance(url_candidate, str) and url_candidate.strip().lower().endswith(".pdf"):
                    return url_candidate.strip()

            # Secondary check: inspect any oa_locations for url_for_pdf
            oa_locations = data.get("oa_locations")
            if isinstance(oa_locations, list):
                for loc in oa_locations:
                    if isinstance(loc, dict):
                        loc_pdf = loc.get("url_for_pdf")
                        if loc_pdf and isinstance(loc_pdf, str) and loc_pdf.strip():
                            return loc_pdf.strip()

            return None
        except Exception as e:
            logger.debug("Unpaywall resolution failed for DOI %s: %s", clean_doi, e)
            return None
