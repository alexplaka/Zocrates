"""
Discovery Engine orchestrating search providers, deduplication, abstract backfill, and OA PDF resolution.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from zocrates.discovery.abstract_fallback import AbstractFallback
from zocrates.discovery.dedup import Deduplicator
from zocrates.discovery.models import CandidatePaper
from zocrates.discovery.providers.base import DEFAULT_POLITE_EMAIL, SearchProvider
from zocrates.discovery.providers.openalex import OpenAlexProvider
from zocrates.discovery.providers.unpaywall import UnpaywallResolver

logger = logging.getLogger(__name__)


class DiscoveryEngine:
    """
    Coordinates scholarly search across providers, enforces strict novelty
    against the local Zotero library, backfills abstracts, and resolves Open Access PDFs.
    """

    def __init__(
        self,
        provider: SearchProvider | None = None,
        deduplicator: Deduplicator | None = None,
        abstract_fallback: AbstractFallback | None = None,
        unpaywall: UnpaywallResolver | None = None,
        email: str = DEFAULT_POLITE_EMAIL,
    ) -> None:
        """
        Args:
            provider: SearchProvider instance (defaults to OpenAlexProvider).
            deduplicator: Deduplicator instance with loaded library state.
            abstract_fallback: AbstractFallback instance for missing abstracts.
            unpaywall: UnpaywallResolver instance for OA PDF links.
            email: Polite pool email for upstream academic APIs.
        """
        self.email = email
        self.provider = provider or OpenAlexProvider(email=email)
        self.deduplicator = deduplicator or Deduplicator()
        self.abstract_fallback = abstract_fallback or AbstractFallback(email=email)
        self.unpaywall = unpaywall or UnpaywallResolver(email=email)

    async def search(
        self,
        query: str,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        is_oa_only: bool = False,
    ) -> list[CandidatePaper]:
        """
        Search for scholarly literature, filtering out papers already in the library
        and enriching top results with abstracts and OA PDF URLs.

        Args:
            query: Free-text search terms or conceptual query.
            limit: Target number of novel candidate papers to return.
            year_from: Optional starting publication year.
            year_to: Optional ending publication year.
            is_oa_only: Restrict results to open-access papers.

        Returns:
            List of novel CandidatePaper instances (at most `limit`).
        """
        q = query.strip() if query else ""
        if not q:
            return []

        # Over-fetch from provider to account for library deduplication
        fetch_limit = min(max(limit * 2, 20), 100)
        try:
            raw_candidates = await self.provider.search(
                query=q,
                limit=fetch_limit,
                year_from=year_from,
                year_to=year_to,
                is_oa_only=is_oa_only,
            )
        except Exception as e:
            logger.warning("Provider search failed for query %r: %s", q, e)
            return []

        if not raw_candidates:
            return []

        # Filter strictly novel papers against local library and batch duplicates
        novel_candidates = self.deduplicator.filter_novel(raw_candidates)

        # Slice to requested limit
        selected = novel_candidates[:limit]
        if not selected:
            return []

        # Opportunistic abstract backfill for selected candidates lacking abstracts
        try:
            selected = await self.abstract_fallback.backfill_batch(selected)
        except Exception as e:
            logger.debug("Abstract backfill encountered error: %s", e)

        # Resolve OA PDF URLs via Unpaywall for OA candidates missing direct PDF URLs
        async def _resolve_oa_pdf(c: CandidatePaper) -> None:
            """Resolve Open Access PDF URL via Unpaywall for a candidate paper.

            Args:
                c: CandidatePaper to inspect and update with resolved PDF URL.
            """
            if not c.oa_pdf_url and c.doi:
                try:
                    pdf_url = await self.unpaywall.resolve_pdf(c.doi)
                    if pdf_url:
                        c.oa_pdf_url = pdf_url
                        c.is_oa = True
                except Exception as ex:
                    logger.debug("Unpaywall resolution failed for DOI %s: %s", c.doi, ex)

        pdf_tasks = [_resolve_oa_pdf(c) for c in selected if not c.oa_pdf_url and c.doi]
        if pdf_tasks:
            await asyncio.gather(*pdf_tasks, return_exceptions=True)

        return selected

    async def resolve_identifier(self, identifier: str) -> tuple[CandidatePaper | None, bool]:
        """
        Resolve a single paper by DOI, arXiv ID, or OpenAlex ID and check its library presence.

        Args:
            identifier: DOI, arXiv ID, or OpenAlex ID string.

        Returns:
            Tuple of (CandidatePaper or None, in_library boolean).
        """
        ident = identifier.strip() if identifier else ""
        if not ident:
            return None, False

        try:
            candidate = await self.provider.resolve_identifier(ident)
        except Exception as e:
            logger.warning("Failed resolving identifier %r: %s", ident, e)
            return None, False

        if candidate is None:
            return None, False

        # Check library presence
        in_library = self.deduplicator.is_in_library(candidate)
        candidate.in_library = in_library

        # Only perform external network lookups if paper is not already in the library
        if not in_library:
            # Backfill abstract if missing
            if not candidate.abstract:
                try:
                    candidate = await self.abstract_fallback.backfill_abstract(candidate)
                except Exception as e:
                    logger.debug("Abstract backfill failed for %r: %s", ident, e)

            # Resolve OA PDF if missing
            if not candidate.oa_pdf_url and candidate.doi:
                try:
                    pdf_url = await self.unpaywall.resolve_pdf(candidate.doi)
                    if pdf_url:
                        candidate.oa_pdf_url = pdf_url
                        candidate.is_oa = True
                except Exception as e:
                    logger.debug("Unpaywall PDF resolution failed for %r: %s", ident, e)

        return candidate, in_library


def get_default_discovery_engine(
    zotero_data_dir: Path | str | None = None,
    polite_email: str | None = None,
    openalex_api_key: str | None = None,
) -> DiscoveryEngine:
    """Instantiate a configured DiscoveryEngine with local Zotero deduplication."""
    from zocrates.discovery.abstract_fallback import AbstractFallback
    from zocrates.discovery.dedup import Deduplicator
    from zocrates.discovery.providers.base import DEFAULT_POLITE_EMAIL
    from zocrates.discovery.providers.openalex import OpenAlexProvider
    from zocrates.discovery.providers.unpaywall import UnpaywallResolver
    from zocrates.config import get_openalex_api_key, get_scholar_polite_email, resolve_zotero_data_dir

    resolved_dir = zotero_data_dir or resolve_zotero_data_dir()
    path_dir = Path(resolved_dir) if resolved_dir else None
    email = polite_email
    if not email:
        try:
            email = get_scholar_polite_email(path_dir)
        except Exception:
            pass
    if not email:
        email = DEFAULT_POLITE_EMAIL

    api_key = openalex_api_key
    if not api_key:
        try:
            api_key = get_openalex_api_key(path_dir)
        except Exception:
            pass

    zotero_db = None
    if resolved_dir:
        sqlite_path = Path(resolved_dir) / "zotero.sqlite"
        storage_dir = Path(resolved_dir) / "storage"
        if sqlite_path.exists():
            try:
                from zocrates.rag.zotero_db import ZoteroDB

                zotero_db = ZoteroDB(sqlite_path=sqlite_path, storage_dir=storage_dir)
            except Exception as e:
                logger.debug("Could not initialize ZoteroDB for discovery deduplication: %s", e)

    deduplicator = Deduplicator(zotero_db=zotero_db)
    provider = OpenAlexProvider(email=email, api_key=api_key)
    abstract_fallback = AbstractFallback(email=email)
    unpaywall = UnpaywallResolver(email=email)

    return DiscoveryEngine(
        provider=provider,
        deduplicator=deduplicator,
        abstract_fallback=abstract_fallback,
        unpaywall=unpaywall,
        email=email,
    )

