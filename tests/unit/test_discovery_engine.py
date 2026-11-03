"""
Unit tests for DiscoveryEngine, AbstractFallback, UnpaywallResolver, and CrossrefValidator.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zocrates.discovery.abstract_fallback import AbstractFallback
from zocrates.discovery.dedup import Deduplicator
from zocrates.discovery.engine import DiscoveryEngine
from zocrates.discovery.models import CandidatePaper
from zocrates.discovery.providers.crossref import CrossrefValidator
from zocrates.discovery.providers.openalex import OpenAlexProvider
from zocrates.discovery.providers.unpaywall import UnpaywallResolver


@pytest.mark.asyncio
async def test_unpaywall_resolver_success():
    resolver = UnpaywallResolver()
    mock_payload = {
        "best_oa_location": {
            "url_for_pdf": "https://example.com/paper.pdf",
            "is_oa": True,
        }
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        pdf = await resolver.resolve_pdf("10.1145/12345")
        assert pdf == "https://example.com/paper.pdf"


@pytest.mark.asyncio
async def test_unpaywall_resolver_404():
    resolver = UnpaywallResolver()
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        pdf = await resolver.resolve_pdf("10.1145/notfound")
        assert pdf is None


@pytest.mark.asyncio
async def test_crossref_validator():
    validator = CrossrefValidator()
    mock_payload = {
        "status": "ok",
        "message": {"title": ["Test Crossref Paper"], "DOI": "10.1145/12345"},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        msg = await validator.validate_doi("10.1145/12345")
        assert msg is not None
        assert msg["title"] == ["Test Crossref Paper"]


@pytest.mark.asyncio
async def test_abstract_fallback_tier1_tier2_tier3():
    fallback = AbstractFallback()

    # Tier 1: Already has abstract -> untouched
    c1 = CandidatePaper(
        candidate_id="c1",
        title="P1",
        abstract="Existing OpenAlex abstract.",
    )
    res1 = await fallback.backfill_abstract(c1)
    assert res1.abstract == "Existing OpenAlex abstract."

    # Tier 2: Missing abstract -> Semantic Scholar backfills
    c2 = CandidatePaper(
        candidate_id="c2",
        title="P2",
        doi="10.1234/s2paper",
        abstract=None,
    )
    with patch.object(fallback, "_fetch_semantic_scholar", new_callable=AsyncMock) as mock_s2:
        mock_s2.return_value = ("S2 backfilled abstract", "One sentence summary")
        res2 = await fallback.backfill_abstract(c2)
        assert res2.abstract == "S2 backfilled abstract"
        assert res2.tldr == "One sentence summary"

    # Tier 3: Neither has abstract -> falls back cleanly with primary topic and keywords
    c3 = CandidatePaper(
        candidate_id="c3",
        title="P3",
        doi="10.1234/empty",
        abstract=None,
        primary_topic="Quantum Computing",
        keywords=["Qubits", "Entanglement"],
    )
    with patch.object(fallback, "_fetch_semantic_scholar", new_callable=AsyncMock) as mock_s2:
        mock_s2.return_value = (None, None)
        res3 = await fallback.backfill_abstract(c3)
        assert res3.abstract is None
        assert res3.primary_topic == "Quantum Computing"
        assert res3.keywords == ["Qubits", "Entanglement"]


@pytest.mark.asyncio
async def test_discovery_engine_search_flow():
    # Setup mock provider returning 3 papers, 1 of which is in the library
    mock_provider = AsyncMock(spec=OpenAlexProvider)
    paper1 = CandidatePaper(candidate_id="p1", title="Existing In Library", doi="10.1000/inlib")
    paper2 = CandidatePaper(candidate_id="p2", title="Novel Paper 2", doi="10.1000/novel2", abstract=None)
    paper3 = CandidatePaper(candidate_id="p3", title="Novel Paper 3", doi="10.1000/novel3", abstract="Already has abs")

    mock_provider.search.return_value = [paper1, paper2, paper3]

    dedup = Deduplicator(existing_dois={"10.1000/inlib"})
    fallback = AbstractFallback()
    unpaywall = UnpaywallResolver()

    with patch.object(fallback, "backfill_batch", new_callable=AsyncMock) as mock_backfill, \
         patch.object(unpaywall, "resolve_pdf", new_callable=AsyncMock) as mock_unpaywall:

        async def _fake_backfill(batch):
            for p in batch:
                if not p.abstract:
                    p.abstract = f"Backfilled for {p.title}"
            return batch

        mock_backfill.side_effect = _fake_backfill
        mock_unpaywall.return_value = "https://oa.org/pdf2.pdf"

        engine = DiscoveryEngine(
            provider=mock_provider,
            deduplicator=dedup,
            abstract_fallback=fallback,
            unpaywall=unpaywall,
        )

        results = await engine.search(query="transformers", limit=10)

        # Paper 1 was filtered out because it is in the library
        assert len(results) == 2
        assert results[0].candidate_id == "p2"
        assert results[0].abstract == "Backfilled for Novel Paper 2"
        assert results[0].oa_pdf_url == "https://oa.org/pdf2.pdf"
        assert results[1].candidate_id == "p3"
        assert results[1].abstract == "Already has abs"


@pytest.mark.asyncio
async def test_discovery_engine_resolve_identifier():
    mock_provider = AsyncMock(spec=OpenAlexProvider)
    novel_paper = CandidatePaper(
        candidate_id="p_novel",
        title="Novel Paper",
        doi="10.1000/novel",
        abstract=None,
    )
    existing_paper = CandidatePaper(
        candidate_id="p_exist",
        title="Existing Paper",
        doi="10.1000/exist",
        abstract="Existing abstract",
    )

    dedup = Deduplicator(existing_dois={"10.1000/exist"})
    engine = DiscoveryEngine(provider=mock_provider, deduplicator=dedup)

    with patch.object(engine.abstract_fallback, "backfill_abstract", new_callable=AsyncMock) as mock_bf, \
         patch.object(engine.unpaywall, "resolve_pdf", new_callable=AsyncMock) as mock_up:
        # 1. Resolve novel paper
        mock_provider.resolve_identifier.return_value = novel_paper
        novel_paper.abstract = "Resolved abstract"
        mock_bf.return_value = novel_paper
        mock_up.return_value = "https://open.org/doc.pdf"

        cand, in_lib = await engine.resolve_identifier("10.1000/novel")
        assert cand is not None
        assert in_lib is False
        assert cand.in_library is False
        assert cand.abstract == "Resolved abstract"
        assert cand.oa_pdf_url == "https://open.org/doc.pdf"
        assert mock_up.call_count == 1

        # 2. Resolve existing paper (in library: skips external backfill and Unpaywall lookups)
        mock_provider.resolve_identifier.return_value = existing_paper
        cand, in_lib = await engine.resolve_identifier("10.1000/exist")
        assert cand is not None
        assert in_lib is True
        assert cand.in_library is True
        # Ensure Unpaywall was not called for existing paper
        assert mock_up.call_count == 1

        # 3. Non-existent paper
        mock_provider.resolve_identifier.return_value = None
        cand, in_lib = await engine.resolve_identifier("10.1000/nonexistent")
        assert cand is None
        assert in_lib is False
