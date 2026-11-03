"""
Opportunistic 3-tier abstract backfill cascade for scholarly candidates.
"""

import asyncio
import logging
from typing import Any
import xml.etree.ElementTree as ET

import httpx

from zocrates.discovery.models import CandidatePaper
from zocrates.discovery.providers.base import DEFAULT_POLITE_EMAIL, make_user_agent
from zocrates.rag.zotero_db import normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)

SEMANTIC_SCHOLAR_URL: str = "https://api.semanticscholar.org/graph/v1/paper"
NCBI_ESEARCH_URL: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_EFETCH_URL: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class AbstractFallback:
    """
    Manages 3-tier opportunistic fallback for paper abstracts:
      - Tier 1: Direct OpenAlex abstract (present in CandidatePaper).
      - Tier 2: Semantic Scholar REST API (and NCBI PubMed E-utilities for biomedical papers) with 2.0s timeout.
      - Tier 3: Clean fallback to OpenAlex keywords and primary topic.
    """

    def __init__(self, email: str = DEFAULT_POLITE_EMAIL, timeout: float = 2.0) -> None:
        """Initialize the abstract fallback resolver.

        Args:
            email: Contact email used for respectful API polite-pool headers.
            timeout: HTTP request timeout in seconds.
        """
        self.email = email
        self.timeout = timeout

    async def _fetch_semantic_scholar(self, identifier: str) -> tuple[str | None, str | None]:
        """
        Query Semantic Scholar Graph API for abstract and TLDR with a tight timeout.

        Args:
            identifier: DOI, arXiv ID, or paper identifier.

        Returns:
            Tuple of (abstract, tldr_text).
        """
        headers = {
            "User-Agent": make_user_agent(self.email),
            "Accept": "application/json",
        }
        params = {"fields": "abstract,tldr"}
        url = f"{SEMANTIC_SCHOLAR_URL}/{identifier}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    abstract = data.get("abstract")
                    tldr_obj = data.get("tldr")
                    tldr_text: str | None = None
                    if isinstance(tldr_obj, dict):
                        tldr_text = tldr_obj.get("text")
                    return (
                        abstract.strip() if abstract and isinstance(abstract, str) else None,
                        tldr_text.strip() if tldr_text and isinstance(tldr_text, str) else None,
                    )
        except Exception as e:
            logger.debug("Semantic Scholar lookup failed for %s: %s", identifier, e)

        return None, None

    async def _fetch_pubmed_abstract(self, doi: str) -> str | None:
        """
        Query NCBI PubMed E-utilities for abstract text by DOI.

        Args:
            doi: Digital Object Identifier.

        Returns:
            Abstract text if found in PubMed, or None.
        """
        headers = {"User-Agent": make_user_agent(self.email)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # 1. Search for PMID by DOI
                search_params = {
                    "db": "pubmed",
                    "term": f"{doi}[doi]",
                    "retmode": "json",
                    "email": self.email,
                }
                search_resp = await client.get(NCBI_ESEARCH_URL, params=search_params, headers=headers)
                if search_resp.status_code != 200:
                    return None

                data = search_resp.json()
                esearch = data.get("esearchresult") or {}
                id_list = esearch.get("idlist") or []
                if not id_list or not isinstance(id_list, list):
                    return None

                pmid = str(id_list[0])

                # 2. Fetch abstract XML for the PMID
                fetch_params = {
                    "db": "pubmed",
                    "id": pmid,
                    "retmode": "xml",
                    "rettype": "abstract",
                    "email": self.email,
                }
                fetch_resp = await client.get(NCBI_EFETCH_URL, params=fetch_params, headers=headers)
                if fetch_resp.status_code != 200:
                    return None

                root = ET.fromstring(fetch_resp.text)
                abstract_texts: list[str] = []
                for elem in root.findall(".//AbstractText"):
                    if elem.text:
                        abstract_texts.append(elem.text.strip())

                if abstract_texts:
                    return " ".join(abstract_texts).strip()
        except Exception as e:
            logger.debug("PubMed abstract lookup failed for DOI %s: %s", doi, e)

        return None

    async def backfill_abstract(self, candidate: CandidatePaper) -> CandidatePaper:
        """
        Backfill missing abstract and TLDR on a candidate paper using opportunistic fallbacks.

        Args:
            candidate: CandidatePaper instance.

        Returns:
            CandidatePaper with abstract/tldr backfilled if resolved.
        """
        # Tier 1: Candidate already has an abstract
        if candidate.abstract and candidate.abstract.strip():
            return candidate

        # Tier 2: Try Semantic Scholar
        doi_clean = normalize_doi(candidate.doi)
        arxiv_clean = normalize_arxiv_id(candidate.arxiv_id)

        s2_identifier: str | None = None
        if doi_clean:
            s2_identifier = f"DOI:{doi_clean}"
        elif arxiv_clean:
            s2_identifier = f"ARXIV:{arxiv_clean}"

        if s2_identifier:
            abs_text, tldr_text = await self._fetch_semantic_scholar(s2_identifier)
            if abs_text:
                candidate.abstract = abs_text
            if tldr_text:
                candidate.tldr = tldr_text

        # If abstract still missing and DOI present, try PubMed for biomedical papers
        if not candidate.abstract and doi_clean:
            is_biomed = False
            if candidate.primary_topic and any(
                term in candidate.primary_topic.lower()
                for term in ("bio", "medicine", "health", "clinical", "pharm", "immun", "neuro")
            ):
                is_biomed = True

            if is_biomed:
                pm_abstract = await self._fetch_pubmed_abstract(doi_clean)
                if pm_abstract:
                    candidate.abstract = pm_abstract

        # Tier 3: If no abstract available, candidate cleanly retains keywords and primary_topic
        return candidate

    async def backfill_batch(self, candidates: list[CandidatePaper]) -> list[CandidatePaper]:
        """
        Backfill missing abstracts concurrently across a batch of candidates.

        Args:
            candidates: List of CandidatePaper instances.

        Returns:
            List of updated CandidatePaper instances.
        """
        tasks = [self.backfill_abstract(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        updated: list[CandidatePaper] = []
        for orig, res in zip(candidates, results, strict=False):
            if isinstance(res, CandidatePaper):
                updated.append(res)
            else:
                updated.append(orig)
        return updated
