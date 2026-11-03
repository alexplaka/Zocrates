"""Fail-open enforcement for the reranker auxiliary model (Phase 7 Stage A).

A missing/invalid ``reranker_model`` config or a rerank HTTP failure must
degrade to returning the pre-rerank evidence with a logged warning — never
fail the chat turn or the search run (plan §5.1 fail-open invariant / §8).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from zocrates.agents.common.reranker import rerank_evidence


def _make_chunks(n: int = 3) -> list[dict]:
    return [
        {"chunk_id": f"c{i}", "text": f"Substantive evidence text number {i}.", "word_count": 6}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_rerank_fail_open_when_model_key_missing():
    """Missing reranker_model key → un-reranked evidence, warning, no HTTP call."""
    chunks = _make_chunks()
    with (
        patch("zocrates.agents.common.reranker.load_zocrates_config", return_value={}),
        patch("zocrates.agents.common.reranker.get_secure_api_key", return_value="k"),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
    ):
        result = await rerank_evidence("query", chunks)

    mock_post.assert_not_called()
    assert [c["chunk_id"] for c in result] == ["c0", "c1", "c2"]
    assert all(c["rerank_score"] is None for c in result)


@pytest.mark.asyncio
async def test_rerank_fail_open_when_model_config_invalid(caplog):
    """Empty / non-string reranker_model config → un-reranked evidence + warning."""
    for bad_value in ("", "   ", None, 123):
        chunks = _make_chunks(2)
        with (
            patch(
                "zocrates.agents.common.reranker.load_zocrates_config",
                return_value={"reranker_model": bad_value},
            ),
            patch("zocrates.agents.common.reranker.get_secure_api_key", return_value="k"),
            patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        ):
            result = await rerank_evidence("query", chunks)
        mock_post.assert_not_called()
        assert [c["chunk_id"] for c in result] == ["c0", "c1"]
        assert all(c["rerank_score"] is None for c in result)


@pytest.mark.asyncio
async def test_rerank_fail_open_on_http_error():
    """Rerank HTTP failure → pre-rerank evidence unchanged, warning logged."""
    chunks = _make_chunks()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=MagicMock(),
        response=MagicMock(status_code=500),
    ))
    with (
        patch(
            "zocrates.agents.common.reranker.load_zocrates_config",
            return_value={"reranker_model": "qwen/qwen3-reranker-8b"},
        ),
        patch("zocrates.agents.common.reranker.get_secure_api_key", return_value="k"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await rerank_evidence("query", chunks)

    # Original order preserved, no exception propagated
    assert [c["chunk_id"] for c in result] == ["c0", "c1", "c2"]
    assert all(c["rerank_score"] is None for c in result)


@pytest.mark.asyncio
async def test_rerank_empty_input_passthrough():
    """Empty evidence list → empty list, no HTTP call."""
    with (
        patch(
            "zocrates.agents.common.reranker.load_zocrates_config",
            return_value={"reranker_model": "qwen/qwen3-reranker-8b"},
        ),
        patch("zocrates.agents.common.reranker.get_secure_api_key", return_value="k"),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
    ):
        result = await rerank_evidence("query", [])
    mock_post.assert_not_called()
    assert result == []


@pytest.mark.asyncio
async def test_rerank_payload_uses_configured_model_verbatim():
    """Slug-resolution audit (§12.5): the configured id is sent verbatim — no prefixing."""
    chunks = _make_chunks(2)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    with (
        patch(
            "zocrates.agents.common.reranker.load_zocrates_config",
            return_value={"reranker_model": "qwen/qwen3-reranker-8b"},
        ),
        patch("zocrates.agents.common.reranker.get_secure_api_key", return_value="k"),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
    ):
        mock_post.return_value = mock_resp
        result = await rerank_evidence("query", chunks)

    assert mock_post.call_args[1]["json"]["model"] == "qwen/qwen3-reranker-8b"
    # Highest score first
    assert result[0]["chunk_id"] == "c0"
    assert result[0]["rerank_score"] == 0.9


@pytest.mark.asyncio
async def test_rerank_fail_open_when_api_key_missing_logs_debug(caplog):
    """When openrouter_api_key is missing, fail open cleanly with debug log."""
    import logging
    chunks = _make_chunks(3)
    with (
        caplog.at_level(logging.DEBUG, logger="zocrates.agents.common.reranker"),
        patch(
            "zocrates.agents.common.reranker.load_zocrates_config",
            return_value={"reranker_model": "qwen/qwen3-reranker-8b"},
        ),
        patch("zocrates.agents.common.reranker.get_secure_api_key", return_value=None),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
    ):
        result = await rerank_evidence("query", chunks)

    mock_post.assert_not_called()
    assert [c["chunk_id"] for c in result] == ["c0", "c1", "c2"]
    assert all(c["rerank_score"] is None for c in result)
    assert any("No OpenRouter API key found" in record.message for record in caplog.records)
