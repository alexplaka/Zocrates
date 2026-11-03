"""
Data models for scholarly literature discovery and batch import staging.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidatePaper:
    """Represents a discovered scholarly paper candidate."""

    candidate_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str = ""
    abstract: str | None = None
    tldr: str | None = None
    is_oa: bool = False
    oa_pdf_url: str | None = None
    oa_pdf_urls: list[str] = field(default_factory=list)
    citation_count: int = 0
    primary_topic: str | None = None
    keywords: list[str] = field(default_factory=list)
    in_library: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize CandidatePaper to a plain dictionary."""
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "openalex_id": self.openalex_id,
            "abstract": self.abstract,
            "tldr": self.tldr,
            "is_oa": self.is_oa,
            "oa_pdf_url": self.oa_pdf_url,
            "oa_pdf_urls": list(self.oa_pdf_urls),
            "citation_count": self.citation_count,
            "primary_topic": self.primary_topic,
            "keywords": list(self.keywords),
            "in_library": self.in_library,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidatePaper":
        """Deserialize CandidatePaper from a dictionary."""
        citation_count_val = data.get("citation_count", 0)
        try:
            citation_count = int(citation_count_val) if citation_count_val is not None else 0
        except (ValueError, TypeError):
            citation_count = 0

        year_val = data.get("year")
        year: int | None = None
        if year_val is not None:
            try:
                year = int(year_val)
            except (ValueError, TypeError):
                year = None

        raw_authors = data.get("authors")
        if isinstance(raw_authors, list):
            authors = [str(a).strip() for a in raw_authors if a and str(a).strip()]
        elif isinstance(raw_authors, str) and raw_authors.strip():
            authors = [raw_authors.strip()]
        else:
            authors = []

        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if k and str(k).strip()]
        elif isinstance(raw_keywords, str) and raw_keywords.strip():
            keywords = [raw_keywords.strip()]
        else:
            keywords = []

        def _clean_opt_str(val: Any) -> str | None:
            """Clean and trim an optional string or scalar value.

            Args:
                val: Input value to convert to a stripped string.

            Returns:
                Stripped string, or None if empty or invalid.
            """
            if val is not None and isinstance(val, (str, int, float)):
                s = str(val).strip()
                return s if s else None
            return None

        raw_urls = data.get("oa_pdf_urls")
        oa_pdf_urls: list[str] = []
        if isinstance(raw_urls, list):
            oa_pdf_urls = [str(u).strip() for u in raw_urls if u and str(u).strip()]
        primary_pdf = _clean_opt_str(data.get("oa_pdf_url"))
        if primary_pdf and primary_pdf not in oa_pdf_urls:
            oa_pdf_urls.insert(0, primary_pdf)

        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            title=str(data.get("title") or "").strip(),
            authors=authors,
            year=year,
            venue=_clean_opt_str(data.get("venue")),
            doi=_clean_opt_str(data.get("doi")),
            arxiv_id=_clean_opt_str(data.get("arxiv_id")),
            openalex_id=str(data.get("openalex_id") or ""),
            abstract=_clean_opt_str(data.get("abstract")),
            tldr=_clean_opt_str(data.get("tldr")),
            is_oa=bool(data.get("is_oa", False)),
            oa_pdf_url=primary_pdf,
            oa_pdf_urls=oa_pdf_urls,
            citation_count=citation_count,
            primary_topic=_clean_opt_str(data.get("primary_topic")),
            keywords=keywords,
            in_library=bool(data.get("in_library", False)),
        )


@dataclass
class StagedItem:
    """Represents a downloaded or staged item ready for Zotero batch import."""

    candidate_id: str
    staged_path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize StagedItem to a plain dictionary."""
        return {
            "candidate_id": self.candidate_id,
            "staged_path": self.staged_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StagedItem":
        """Deserialize StagedItem from a dictionary."""
        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            staged_path=str(data.get("staged_path") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
