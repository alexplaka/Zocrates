"""
Unit tests for OpenAlexProvider, abstract reconstruction, and arXiv extraction.
"""

from unittest.mock import AsyncMock, patch

import pytest

from zocrates.discovery.providers.base import TokenBucketRateLimiter
from zocrates.discovery.providers.openalex import OpenAlexProvider


def test_reconstruct_abstract():
    inverted = {
        "Attention": [0],
        "is": [1],
        "all": [2],
        "you": [3],
        "need": [4],
    }
    assert OpenAlexProvider.reconstruct_abstract(inverted) == "Attention is all you need"

    disordered = {
        "networks": [4],
        "Deep": [0],
        "residual": [1],
        "learning": [2],
        "for": [3],
    }
    assert (
        OpenAlexProvider.reconstruct_abstract(disordered)
        == "Deep residual learning for networks"
    )

    assert OpenAlexProvider.reconstruct_abstract(None) is None
    assert OpenAlexProvider.reconstruct_abstract({}) is None
    assert OpenAlexProvider.reconstruct_abstract({"word": []}) is None


def test_extract_arxiv_id_from_work():
    # 1. From landing_page_url
    work1 = {
        "primary_location": {"landing_page_url": "https://arxiv.org/abs/2305.12345v3"},
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work1) == "2305.12345"

    # 2. From locations pdf_url
    work2 = {
        "locations": [
            {"pdf_url": "https://arxiv.org/pdf/2305.99999.pdf"},
        ]
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work2) == "2305.99999"

    # 3. From DataCite DOI
    work3 = {
        "doi": "https://doi.org/10.48550/arXiv.2401.12345",
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work3) == "2401.12345"

    # 4. From ids.arxiv
    work4 = {
        "ids": {"arxiv": "https://arxiv.org/abs/1706.03762v5"},
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work4) == "1706.03762"

    # 5. Legacy arXiv identifier
    work5 = {
        "primary_location": {"landing_page_url": "https://arxiv.org/abs/hep-th/9901001"},
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work5) == "hep-th/9901001"

    # 6. No arXiv
    work6 = {
        "doi": "https://doi.org/10.1038/nature12345",
        "primary_location": {"landing_page_url": "https://nature.com/articles/123"},
    }
    assert OpenAlexProvider.extract_arxiv_id_from_work(work6) is None


def test_parse_work():
    provider = OpenAlexProvider()
    work = {
        "id": "https://openalex.org/W12345",
        "title": "Transformers in Action",
        "authorships": [
            {"author": {"display_name": "Alice Smith"}},
            {"author": {"display_name": "Bob Jones"}},
        ],
        "publication_year": 2023,
        "primary_location": {
            "source": {"display_name": "NeurIPS"},
            "pdf_url": "https://neurips.cc/paper.pdf",
            "landing_page_url": "https://arxiv.org/abs/2305.12345v1",
        },
        "doi": "https://doi.org/10.5555/neurips2023",
        "open_access": {"is_oa": True, "oa_url": "https://neurips.cc/paper.pdf"},
        "cited_by_count": 120,
        "abstract_inverted_index": {"We": [0], "study": [1], "transformers": [2]},
        "primary_topic": {"display_name": "Natural Language Processing"},
        "keywords": [{"display_name": "Transformers"}, {"display_name": "Self-Attention"}],
    }

    candidate = provider._parse_work(work)
    assert candidate.candidate_id == "https://openalex.org/W12345"
    assert candidate.title == "Transformers in Action"
    assert candidate.authors == ["Alice Smith", "Bob Jones"]
    assert candidate.year == 2023
    assert candidate.venue == "NeurIPS"
    assert candidate.doi == "10.5555/neurips2023"
    assert candidate.arxiv_id == "2305.12345"
    assert candidate.abstract == "We study transformers"
    assert candidate.is_oa is True
    assert candidate.oa_pdf_url == "https://neurips.cc/paper.pdf"
    assert candidate.citation_count == 120
    assert candidate.primary_topic == "Natural Language Processing"
    assert candidate.keywords == ["Transformers", "Self-Attention"]


@pytest.mark.asyncio
async def test_search_openalex_filters():
    provider = OpenAlexProvider()
    mock_payload = {
        "results": [
            {
                "id": "https://openalex.org/W1",
                "title": "Paper 1",
                "publication_year": 2021,
                "authorships": [],
            }
        ]
    }

    with patch.object(provider, "_get_with_retry", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_payload

        # Year range and OA filter
        results = await provider.search(
            query="speculative decoding",
            limit=5,
            year_from=2020,
            year_to=2023,
            is_oa_only=True,
        )

        assert len(results) == 1
        assert results[0].title == "Paper 1"
        assert mock_get.called
        call_args = mock_get.call_args
        params = call_args[1]["params"]
        assert params["search"] == "speculative decoding"
        assert params["per-page"] == 5
        assert "is_oa:true" in params["filter"]
        assert "publication_year:2020-2023" in params["filter"]


@pytest.mark.asyncio
async def test_resolve_identifier_doi():
    provider = OpenAlexProvider()
    mock_work = {
        "id": "https://openalex.org/W2",
        "title": "DOI Resolved Paper",
        "doi": "https://doi.org/10.1145/123456",
        "publication_year": 2022,
    }
    with patch.object(provider, "_get_with_retry", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"results": [mock_work]}

        cand = await provider.resolve_identifier("10.1145/123456")
        assert cand is not None
        assert cand.title == "DOI Resolved Paper"
        assert cand.doi == "10.1145/123456"


@pytest.mark.asyncio
async def test_resolve_identifier_arxiv():
    provider = OpenAlexProvider()
    mock_work = {
        "id": "https://openalex.org/W3",
        "title": "arXiv Resolved Paper",
        "primary_location": {"landing_page_url": "https://arxiv.org/abs/2305.12345v1"},
        "publication_year": 2023,
    }
    with patch.object(provider, "_get_with_retry", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"results": [mock_work]}

        cand = await provider.resolve_identifier("2305.12345")
        assert cand is not None
        assert cand.title == "arXiv Resolved Paper"
        assert cand.arxiv_id == "2305.12345"


@pytest.mark.asyncio
async def test_rate_limiter_acquire():
    limiter = TokenBucketRateLimiter(rate=50.0, capacity=10.0)
    await limiter.acquire(1.0)
    assert limiter.tokens < 10.0


def test_openalex_provider_api_key_initialization():
    # 1. Strips whitespace from provided key
    p1 = OpenAlexProvider(api_key="  test_key_123  ")
    assert p1.api_key == "test_key_123"

    # 2. Empty string or None results in None
    p2 = OpenAlexProvider(api_key="")
    assert p2.api_key is None

    p3 = OpenAlexProvider(api_key=None)
    assert p3.api_key is None


@pytest.mark.asyncio
async def test_openalex_provider_auth_header():
    from unittest.mock import MagicMock

    # 1. Provider with API key sets Authorization Bearer header
    provider_with_key = OpenAlexProvider(api_key="secret-openalex-key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": []}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider_with_key._get_with_retry("https://api.openalex.org/works")
        assert res == {"results": []}
        assert mock_get.called
        headers = mock_get.call_args[1]["headers"]
        assert headers.get("Authorization") == "Bearer secret-openalex-key"

    # 2. Provider without API key does not set Authorization header
    provider_no_key = OpenAlexProvider(api_key=None)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider_no_key._get_with_retry("https://api.openalex.org/works")
        assert res == {"results": []}
        assert mock_get.called
        headers = mock_get.call_args[1]["headers"]
        assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_openalex_provider_401_403_no_retry():
    from unittest.mock import MagicMock

    provider = OpenAlexProvider(api_key="rejected-key")

    for status in (401, 403):
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = "Forbidden or Unauthorized"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await provider._get_with_retry("https://api.openalex.org/works")
            assert res is None
            # Must return immediately without retries
            assert mock_get.call_count == 1


def test_openalex_api_key_config_helpers(monkeypatch):
    from zocrates.config import get_openalex_api_key, save_openalex_api_key

    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    assert get_openalex_api_key() is None

    save_openalex_api_key("openalex-env-key-999")
    assert get_openalex_api_key() == "openalex-env-key-999"

    save_openalex_api_key("")
    assert get_openalex_api_key() is None


def test_get_default_discovery_engine_openalex_key():
    from zocrates.discovery.engine import get_default_discovery_engine

    engine = get_default_discovery_engine(openalex_api_key="param_key_123")
    assert engine.provider.api_key == "param_key_123"

