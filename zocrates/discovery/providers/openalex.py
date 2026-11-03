"""
OpenAlex scholarly literature search provider and identifier resolver.
"""

import asyncio
import hashlib
import logging
import re
from typing import Any

import httpx

from zocrates.discovery.models import CandidatePaper
from zocrates.discovery.providers.base import (
    DEFAULT_POLITE_EMAIL,
    SearchProvider,
    TokenBucketRateLimiter,
    make_user_agent,
)
from zocrates.rag.zotero_db import normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)

OPENALEX_WORKS_URL: str = "https://api.openalex.org/works"
OPENALEX_SELECT_FIELDS: str = (
    "id,doi,title,display_name,authorships,publication_year,primary_location,"
    "open_access,abstract_inverted_index,cited_by_count,type,ids,primary_topic,"
    "keywords,locations"
)


class OpenAlexProvider(SearchProvider):
    """
    Primary search provider backed by the OpenAlex REST API.
    Indexes 250M+ scholarly works, arXiv preprints, and open-access metadata.
    """

    def __init__(
        self,
        email: str = DEFAULT_POLITE_EMAIL,
        rate_limiter: TokenBucketRateLimiter | None = None,
        timeout: float = 15.0,
        api_key: str | None = None,
    ) -> None:
        """Initialize the OpenAlex search provider.

        Args:
            email: Contact email for polite-pool API requests.
            rate_limiter: Token bucket rate limiter instance.
            timeout: HTTP request timeout in seconds.
            api_key: Optional OpenAlex premium API key.
        """
        self.email = email
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(rate=10.0, capacity=10.0)
        self.timeout = timeout
        self.api_key = api_key.strip() if api_key and isinstance(api_key, str) else None

    @staticmethod
    def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
        """
        Reconstruct plain-text abstract from an OpenAlex abstract_inverted_index dictionary.

        Args:
            inverted_index: Mapping of words to their token position indices.

        Returns:
            Coherent, human-readable abstract string, or None if empty/invalid.
        """
        if not inverted_index or not isinstance(inverted_index, dict):
            return None

        positions: list[tuple[int, str]] = []
        for word, pos_list in inverted_index.items():
            if not isinstance(pos_list, list):
                continue
            for pos in pos_list:
                if isinstance(pos, int):
                    positions.append((pos, word))

        if not positions:
            return None

        positions.sort(key=lambda x: x[0])
        return " ".join(word for _, word in positions).strip() or None

    @staticmethod
    def extract_arxiv_id_from_work(work: dict[str, Any]) -> str | None:
        """
        Extract canonical arXiv ID without version suffix from an OpenAlex work record.

        Args:
            work: OpenAlex work dictionary.

        Returns:
            Canonical lowercase arXiv ID without 'v' suffix (e.g. '2305.12345'), or None.
        """
        # 1. Check locations landing_page_url and pdf_url
        locations = work.get("locations") or []
        primary_loc = work.get("primary_location") or {}
        candidate_locs: list[dict[str, Any]] = [primary_loc]
        if isinstance(locations, list):
            candidate_locs.extend(locations)

        for loc in candidate_locs:
            if not isinstance(loc, dict):
                continue
            for url_key in ("landing_page_url", "pdf_url"):
                val = loc.get(url_key)
                if val and isinstance(val, str):
                    cand = normalize_arxiv_id(val)
                    if cand:
                        return cand

        # 2. Check DataCite DOIs matching 10.48550/arxiv.(...)
        doi = work.get("doi")
        if doi and isinstance(doi, str):
            cand_doi = normalize_arxiv_id(doi)
            if cand_doi:
                return cand_doi

        # 3. Check ids dictionary if present
        ids = work.get("ids") or {}
        if isinstance(ids, dict):
            arxiv_val = ids.get("arxiv")
            if arxiv_val and isinstance(arxiv_val, str):
                cand_ids = normalize_arxiv_id(arxiv_val)
                if cand_ids:
                    return cand_ids

        return None

    def _parse_work(self, work: dict[str, Any]) -> CandidatePaper:
        """Parse an OpenAlex work dictionary into a CandidatePaper dataclass."""
        openalex_id = str(work.get("id") or "").strip()
        candidate_id = openalex_id or hashlib.sha1((work.get("title") or work.get("display_name") or "").encode("utf-8")).hexdigest()[:16]

        # Clean title
        title = str(work.get("title") or work.get("display_name") or "").strip()

        # Authors
        authors: list[str] = []
        authorships = work.get("authorships") or []
        if isinstance(authorships, list):
            for a in authorships:
                if isinstance(a, dict):
                    author = a.get("author") or {}
                    name = author.get("display_name") or a.get("raw_author_name")
                    if name and isinstance(name, str) and name.strip():
                        authors.append(name.strip())

        # Year
        year_raw = work.get("publication_year")
        year: int | None = None
        if year_raw is not None:
            try:
                year = int(year_raw)
            except (ValueError, TypeError):
                year = None

        # Venue / Host Source
        venue: str | None = None
        primary_loc = work.get("primary_location") or {}
        if isinstance(primary_loc, dict):
            source = primary_loc.get("source") or {}
            if isinstance(source, dict):
                venue = source.get("display_name") or source.get("name")
        if not venue:
            host_venue = work.get("host_venue") or {}
            if isinstance(host_venue, dict):
                venue = host_venue.get("display_name") or host_venue.get("name")

        # DOI
        raw_doi = work.get("doi")
        doi = normalize_doi(raw_doi)

        # arXiv ID
        arxiv_id = self.extract_arxiv_id_from_work(work)

        # Abstract
        abstract = self.reconstruct_abstract(work.get("abstract_inverted_index"))

        # Open Access & OA PDF URLs (primary and alternatives)
        oa_dict = work.get("open_access") or {}
        is_oa = bool(oa_dict.get("is_oa", False)) if isinstance(oa_dict, dict) else False

        oa_pdf_urls: list[str] = []
        best_oa = work.get("best_oa_location") or {}
        if isinstance(best_oa, dict) and best_oa.get("pdf_url"):
            u = str(best_oa["pdf_url"]).strip()
            if u and u not in oa_pdf_urls:
                oa_pdf_urls.append(u)
        if isinstance(primary_loc, dict) and primary_loc.get("pdf_url"):
            u = str(primary_loc["pdf_url"]).strip()
            if u and u not in oa_pdf_urls:
                oa_pdf_urls.append(u)
        if isinstance(oa_dict, dict) and oa_dict.get("oa_url"):
            u = str(oa_dict["oa_url"]).strip()
            if u and u not in oa_pdf_urls:
                oa_pdf_urls.append(u)

        locations = work.get("locations") or []
        if isinstance(locations, list):
            for loc in locations:
                if isinstance(loc, dict) and loc.get("is_oa"):
                    for k in ("pdf_url", "landing_page_url"):
                        raw_u = loc.get(k)
                        if raw_u and isinstance(raw_u, str):
                            u = raw_u.strip()
                            if u and u not in oa_pdf_urls:
                                oa_pdf_urls.append(u)

        oa_pdf_url = oa_pdf_urls[0] if oa_pdf_urls else None

        # Citation count
        citation_count_val = work.get("cited_by_count", 0)
        try:
            citation_count = int(citation_count_val) if citation_count_val is not None else 0
        except (ValueError, TypeError):
            citation_count = 0

        # Primary topic
        primary_topic: str | None = None
        topic_dict = work.get("primary_topic") or {}
        if isinstance(topic_dict, dict):
            primary_topic = topic_dict.get("display_name")

        # Keywords
        keywords: list[str] = []
        raw_kws = work.get("keywords") or []
        if isinstance(raw_kws, list):
            for kw in raw_kws:
                if isinstance(kw, dict):
                    name = kw.get("display_name")
                    if name and isinstance(name, str) and name.strip():
                        keywords.append(name.strip())
                elif isinstance(kw, str) and kw.strip():
                    keywords.append(kw.strip())

        return CandidatePaper(
            candidate_id=candidate_id,
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            arxiv_id=arxiv_id,
            openalex_id=openalex_id,
            abstract=abstract,
            tldr=None,
            is_oa=is_oa,
            oa_pdf_url=oa_pdf_url,
            oa_pdf_urls=oa_pdf_urls,
            citation_count=citation_count,
            primary_topic=primary_topic,
            keywords=keywords,
            in_library=False,
        )

    async def _get_with_retry(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any] | None:
        """Execute GET request with token-bucket throttling and 429 exponential backoff."""
        await self.rate_limiter.acquire()
        headers = {
            "User-Agent": make_user_agent(self.email),
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        backoff = 1.0

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code in (401, 403):
                        logger.warning(
                            "OpenAlex rejected API key: HTTP %d for %s: %s",
                            resp.status_code,
                            url,
                            resp.text[:200],
                        )
                        return None
                    elif resp.status_code == 429:
                        if attempt == max_retries:
                            logger.warning("OpenAlex 429 rate limit exceeded max retries")
                            return None
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after and retry_after.strip().isdigit():
                            wait_sec = min(float(retry_after.strip()), 10.0)
                        else:
                            wait_sec = backoff
                        logger.info("OpenAlex 429 rate limited, backing off for %.1f s", wait_sec)
                        await asyncio.sleep(wait_sec)
                        backoff *= 2.0
                        continue
                    elif resp.status_code == 404:
                        logger.debug("OpenAlex returned 404 for %s", url)
                        return None
                    else:
                        logger.warning(
                            "OpenAlex HTTP error %d for %s: %s",
                            resp.status_code,
                            url,
                            resp.text[:200],
                        )
                        return None
            except Exception as e:
                if attempt == max_retries:
                    logger.warning("OpenAlex request failed after %d retries: %s", max_retries, e)
                    return None
                await asyncio.sleep(backoff)
                backoff *= 2.0

        return None

    async def search(
        self,
        query: str,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        is_oa_only: bool = False,
    ) -> list[CandidatePaper]:
        """
        Execute free-text or conceptual search against OpenAlex works.

        Args:
            query: User search query string.
            limit: Maximum candidate works to return.
            year_from: Optional minimum publication year.
            year_to: Optional maximum publication year.
            is_oa_only: Restrict to open-access papers.

        Returns:
            List of parsed CandidatePaper instances.
        """
        q = query.strip() if query else ""
        if not q:
            return []

        # If query looks like an exact DOI or arXiv ID, delegate directly to resolve_identifier
        clean_doi = normalize_doi(q)
        if clean_doi and ("/" in clean_doi) and clean_doi.startswith("10."):
            single = await self.resolve_identifier(clean_doi)
            return [single] if single else []

        clean_arxiv = normalize_arxiv_id(q)
        if clean_arxiv and re.match(r"^(\d{4}\.\d{4,5}|[a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?\/\d{7})$", clean_arxiv):
            single = await self.resolve_identifier(clean_arxiv)
            return [single] if single else []

        per_page = min(max(1, limit), 100)
        params: dict[str, Any] = {
            "search": q,
            "per-page": per_page,
            "select": OPENALEX_SELECT_FIELDS,
        }

        # Build filter expressions
        filters: list[str] = []
        if is_oa_only:
            filters.append("is_oa:true")

        if year_from is not None and year_to is not None:
            if year_from == year_to:
                filters.append(f"publication_year:{year_from}")
            else:
                filters.append(f"publication_year:{year_from}-{year_to}")
        elif year_from is not None:
            filters.append(f"publication_year:>{year_from - 1}")
        elif year_to is not None:
            filters.append(f"publication_year:<{year_to + 1}")

        if filters:
            params["filter"] = ",".join(filters)

        data = await self._get_with_retry(OPENALEX_WORKS_URL, params=params)
        if not data:
            return []

        results = data.get("results") or []
        candidates: list[CandidatePaper] = []
        if isinstance(results, list):
            for work in results:
                if isinstance(work, dict):
                    candidates.append(self._parse_work(work))

        return candidates[:limit]

    async def resolve_identifier(self, identifier: str) -> CandidatePaper | None:
        """
        Resolve a paper by DOI, arXiv ID, or OpenAlex ID.

        Args:
            identifier: DOI, arXiv ID, or OpenAlex ID.

        Returns:
            Resolved CandidatePaper, or None if not found.
        """
        ident = identifier.strip() if identifier else ""
        if not ident:
            return None

        # 1. OpenAlex ID (e.g. "https://openalex.org/W..." or "W123456789")
        m_oa = re.search(r"\b(W\d+)\b", ident, re.IGNORECASE)
        if m_oa:
            oa_id = m_oa.group(1).upper()
            url = f"{OPENALEX_WORKS_URL}/{oa_id}"
            params = {"select": OPENALEX_SELECT_FIELDS}
            data = await self._get_with_retry(url, params=params)
            if data and isinstance(data, dict) and "id" in data:
                return self._parse_work(data)

        # 2. DOI resolution
        clean_doi = normalize_doi(ident)
        if clean_doi and clean_doi.startswith("10.") and "/" in clean_doi:
            # If it's a DataCite arXiv DOI, extract the arXiv ID as fallback
            arxiv_from_doi = normalize_arxiv_id(clean_doi)

            # Query OpenAlex with canonical DOI filter
            params = {
                "filter": f"doi:https://doi.org/{clean_doi}",
                "select": OPENALEX_SELECT_FIELDS,
                "per-page": 1,
            }
            data = await self._get_with_retry(OPENALEX_WORKS_URL, params=params)
            if data and isinstance(data, dict):
                results = data.get("results") or []
                if isinstance(results, list) and results:
                    return self._parse_work(results[0])

            # If DOI query didn't find it but it's an arXiv DOI, fall through to arXiv lookup
            if arxiv_from_doi:
                ident = arxiv_from_doi

        # 3. arXiv ID resolution
        clean_arxiv = normalize_arxiv_id(ident)
        if clean_arxiv:
            # First try DataCite DOI filter
            datacite_doi = f"https://doi.org/10.48550/arxiv.{clean_arxiv}"
            params = {
                "filter": f"doi:{datacite_doi}",
                "select": OPENALEX_SELECT_FIELDS,
                "per-page": 1,
            }
            data = await self._get_with_retry(OPENALEX_WORKS_URL, params=params)
            if data and isinstance(data, dict):
                results = data.get("results") or []
                if isinstance(results, list) and results:
                    return self._parse_work(results[0])

            # Secondary search for landing page or arxiv ID text
            params_search = {
                "search": clean_arxiv,
                "select": OPENALEX_SELECT_FIELDS,
                "per-page": 5,
            }
            data_search = await self._get_with_retry(OPENALEX_WORKS_URL, params=params_search)
            if data_search and isinstance(data_search, dict):
                results = data_search.get("results") or []
                if isinstance(results, list):
                    for w in results:
                        if isinstance(w, dict) and self.extract_arxiv_id_from_work(w) == clean_arxiv:
                            return self._parse_work(w)

        return None
