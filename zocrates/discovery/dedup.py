"""
In-memory deduplication and novelty filtering against local Zotero library and search batches.
"""

import difflib
import logging
import re
from typing import Any

from zocrates.discovery.models import CandidatePaper
from zocrates.rag.zotero_db import normalize_arxiv_id, normalize_doi

logger = logging.getLogger(__name__)


def normalize_title(title: str | None) -> str:
    """
    Normalize paper title for fuzzy comparison by lowercasing,
    stripping punctuation, and normalizing whitespace.

    Args:
        title: Raw title string.

    Returns:
        Clean, lowercase, punctuation-free string.
    """
    if not title:
        return ""
    # Lowercase
    s = title.lower()
    # Replace punctuation symbols with whitespace
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse multiple whitespaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


class Deduplicator:
    """
    Enforces strict novelty against the user's local Zotero library and
    removes duplicates within search result batches.
    """

    def __init__(
        self,
        zotero_db: Any | None = None,
        existing_dois: set[str] | None = None,
        existing_arxiv_ids: set[str] | None = None,
        existing_records: list[dict[str, Any]] | None = None,
        existing_titles: list[str] | None = None,
    ) -> None:
        """
        Initialize Deduplicator with library state from ZoteroDB or explicit collections.

        Args:
            zotero_db: Optional ZoteroDB instance to query live library records.
            existing_dois: Pre-populated set of normalized DOIs.
            existing_arxiv_ids: Pre-populated set of normalized arXiv IDs.
            existing_records: Pre-populated list of library record dicts (with title, year, creators).
            existing_titles: Pre-populated list of raw or normalized titles.
        """
        self.existing_dois: set[str] = set(existing_dois) if existing_dois else set()
        self.existing_arxiv_ids: set[str] = set(existing_arxiv_ids) if existing_arxiv_ids else set()
        self.existing_records: list[dict[str, Any]] = list(existing_records) if existing_records else []

        if existing_titles:
            for t in existing_titles:
                norm_t = normalize_title(t)
                if norm_t:
                    self.existing_records.append({
                        "title": t,
                        "normalized_title": norm_t,
                        "year": None,
                        "creators": [],
                    })

        # Load from ZoteroDB if provided
        if zotero_db is not None:
            self._load_from_zotero_db(zotero_db)

        # Precompute normalized titles on existing records
        for rec in self.existing_records:
            if "normalized_title" not in rec and rec.get("title"):
                rec["normalized_title"] = normalize_title(rec["title"])

    def _load_from_zotero_db(self, zotero_db: Any) -> None:
        """Query ZoteroDB for active library DOIs, arXiv IDs, and bibliographic records."""
        try:
            if hasattr(zotero_db, "get_all_dois"):
                self.existing_dois.update(zotero_db.get_all_dois())
            if hasattr(zotero_db, "get_all_arxiv_ids"):
                self.existing_arxiv_ids.update(zotero_db.get_all_arxiv_ids())
            if hasattr(zotero_db, "get_all_dedup_records"):
                records = zotero_db.get_all_dedup_records()
                for rec in records:
                    rec["normalized_title"] = normalize_title(rec.get("title"))
                    self.existing_records.append(rec)
                    if rec.get("doi"):
                        self.existing_dois.add(rec["doi"])
                    if rec.get("arxiv_id"):
                        self.existing_arxiv_ids.add(rec["arxiv_id"])
            elif hasattr(zotero_db, "get_all_titles"):
                titles = zotero_db.get_all_titles()
                for t in titles:
                    norm = normalize_title(t)
                    if norm:
                        self.existing_records.append({
                            "title": t,
                            "normalized_title": norm,
                            "year": None,
                            "creators": [],
                        })
        except Exception as e:
            logger.warning("Failed to load deduplication records from ZoteroDB: %s", e)

    def is_in_library(self, candidate: CandidatePaper) -> bool:
        """
        Check whether candidate paper already exists in the user's library.

        Evaluates:
          1. DOI exact match.
          2. arXiv ID exact match.
          3. Exact normalized title match.
          4. Fuzzy title similarity (>= 0.92) with matching year or author.

        Args:
            candidate: CandidatePaper instance.

        Returns:
            True if the paper exists in the library, False otherwise.
        """
        # 1. Exact DOI match
        if candidate.doi:
            cand_doi = normalize_doi(candidate.doi)
            if cand_doi and cand_doi in self.existing_dois:
                return True

        # 2. Exact arXiv ID match
        if candidate.arxiv_id:
            cand_arxiv = normalize_arxiv_id(candidate.arxiv_id)
            if cand_arxiv and cand_arxiv in self.existing_arxiv_ids:
                return True

        # 3. Title matching (exact and fuzzy)
        cand_norm = normalize_title(candidate.title)
        if not cand_norm or len(cand_norm) < 4:
            return False

        cand_tokens = set(cand_norm.split())

        for rec in self.existing_records:
            rec_norm = rec.get("normalized_title") or normalize_title(rec.get("title"))
            if not rec_norm:
                continue

            # Exact normalized title match
            if cand_norm == rec_norm:
                return True

            rec_tokens = set(rec_norm.split())
            token_intersection = cand_tokens.intersection(rec_tokens)
            token_jaccard = (
                len(token_intersection) / len(cand_tokens.union(rec_tokens))
                if cand_tokens or rec_tokens
                else 0.0
            )
            min_token_len = min(len(cand_tokens), len(rec_tokens))
            token_containment = (
                len(token_intersection) / min_token_len
                if min_token_len > 0
                else 0.0
            )

            similarity = difflib.SequenceMatcher(None, cand_norm, rec_norm).ratio()
            if similarity >= 0.98:
                return True

            if (
                similarity >= 0.88
                or token_jaccard >= 0.80
                or (token_containment >= 0.85 and min_token_len >= 4)
            ):
                cand_year = candidate.year
                rec_year = rec.get("year")
                year_match = False
                if cand_year is not None and rec_year is not None:
                    if abs(cand_year - rec_year) <= 1:
                        year_match = True
                elif cand_year is None or rec_year is None:
                    year_match = True

                author_match = False
                cand_authors = candidate.authors
                rec_creators = rec.get("creators") or []
                if cand_authors and rec_creators:
                    cand_surnames = {a.split()[-1].lower() for a in cand_authors if a.strip()}
                    rec_surnames = {
                        (c.split(",")[0] if "," in c else c.split()[-1]).strip().lower()
                        for c in rec_creators
                        if c.strip()
                    }
                    if cand_surnames.intersection(rec_surnames):
                        author_match = True

                if (
                    (year_match and author_match)
                    or (similarity >= 0.92 and (year_match or author_match))
                    or (token_containment >= 0.95 and (year_match or author_match))
                ):
                    return True

        return False

    def filter_novel(self, candidates: list[CandidatePaper]) -> list[CandidatePaper]:
        """
        Filter candidate papers enforcing strict novelty against the library
        and deduplicating across the current batch.

        Args:
            candidates: List of CandidatePaper instances.

        Returns:
            List of strictly novel CandidatePaper instances, deduplicated intra-batch.
        """
        novel: list[CandidatePaper] = []
        seen_dois: set[str] = set()
        seen_arxiv_ids: set[str] = set()
        seen_titles: set[str] = set()

        for c in candidates:
            # 1. Strict novelty check against existing library
            if self.is_in_library(c):
                continue

            # 2. Intra-batch deduplication
            c_doi = normalize_doi(c.doi)
            c_arxiv = normalize_arxiv_id(c.arxiv_id)
            c_title = normalize_title(c.title)

            if c_doi and c_doi in seen_dois:
                continue
            if c_arxiv and c_arxiv in seen_arxiv_ids:
                continue
            if c_title and c_title in seen_titles:
                continue

            if c_doi:
                seen_dois.add(c_doi)
            if c_arxiv:
                seen_arxiv_ids.add(c_arxiv)
            if c_title:
                seen_titles.add(c_title)

            novel.append(c)

        return novel
