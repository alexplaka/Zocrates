"""Unit tests for the local Laya citation-verification provider path (Phase 7 Stage A).

Covers:
  - Config acceptance via ``is_local_decision_model`` (aliases: "local_laya", "laya", "local").
  - ``verify_citations_support`` routing to the local Laya URL in batch mode
    when the daemon is healthy.
  - Graceful fail-open behavior when the daemon is down (no exceptions, all
    probabilities None).

NOTE (live user checklist — NOT executed here): the real daemon is installed and
started with ``uv tool install --with uvicorn --with fastapi laya`` followed by
``laya-serve`` (or via ``POST /llm/decision_tool/install`` + ``/start``); these
tests only exercise the client side with stubs/mocks.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from zocrates.agents.library_search.citation_verifier import (
    _check_support_local_laya,
    verify_citations_support,
)
from zocrates.config import is_local_decision_model

LOCAL_URL = "http://127.0.0.1:8005/v1/systemone"

LAYA_CONFIG = {
    "citation_verification_provider": "local_laya",
    "citation_verification_model": "laya",
    "citation_verification_local_url": LOCAL_URL,
    "citation_verification_threshold": 0.60,
    "citation_verification_timeout_sec": 10.0,
    "citation_verification_turn_budget_sec": 20.0,
}


@pytest.fixture
def sample_chunks():
    return [
        {
            "chunk_id": "PAPER::1",
            "item_key": "KEY1",
            "title": "Sample Paper",
            "authors": ["Doe, Jane"],
            "year": "2024",
            "heading": "Results",
            "text": "The treatment group showed a statistically significant improvement.",
            "chunk_type": "text",
        }
    ]


def test_is_local_decision_model_accepts_local_laya_aliases():
    assert is_local_decision_model("local_laya") is True
    assert is_local_decision_model("laya") is True
    assert is_local_decision_model("local") is True
    assert is_local_decision_model("local/laya") is True
    assert is_local_decision_model("local:laya") is True
    assert is_local_decision_model("inception/mercury-decide") is False
    assert is_local_decision_model(None) is False
    assert is_local_decision_model("") is False


@pytest.mark.asyncio
async def test_verify_citations_local_laya_routes_to_local_batch_endpoint(sample_chunks):
    """Healthy daemon: provider routes to the local URL in batch execution mode."""
    text = "The treatment group showed a statistically significant improvement [1]."
    batch_res = {(1, 1): 0.92}

    with (
        patch(
            "zocrates.agents.library_search.citation_verifier._probe_local_laya_health",
            return_value=True,
        ) as mock_health,
        patch(
            "zocrates.agents.library_search.citation_verifier._check_support_local_laya_batch",
            return_value=batch_res,
        ) as mock_batch,
    ):
        telemetry: dict = {}
        results = await verify_citations_support(
            text, sample_chunks, config=dict(LAYA_CONFIG), telemetry_out=telemetry
        )

    # Health probe hit the daemon's /health endpoint derived from the local URL
    assert mock_health.call_args[0][0] == "http://127.0.0.1:8005/health"
    # Batch client received the local systemone URL
    assert mock_batch.call_args[1]["local_url"] == LOCAL_URL
    assert results == {1: [0.92]}
    assert telemetry["provider"] == "local_laya"
    assert telemetry["model"] == "laya"
    assert telemetry["endpoint"] == LOCAL_URL
    assert telemetry["execution_mode"] == "batch"


@pytest.mark.asyncio
async def test_verify_citations_local_laya_fails_open_when_daemon_down(sample_chunks):
    """Daemon down: health probe fails → all probabilities None, no exception."""
    text = "The treatment group showed a statistically significant improvement [1]."
    with patch(
        "zocrates.agents.library_search.citation_verifier._probe_local_laya_health",
        return_value=False,
    ):
        telemetry: dict = {}
        results = await verify_citations_support(
            text, sample_chunks, config=dict(LAYA_CONFIG), telemetry_out=telemetry
        )
    assert results == {1: [None]}
    assert telemetry["summary"]["fail_open"] == 1
    assert telemetry["assessments"][0]["verdict"] == "FAIL_OPEN"


@pytest.mark.asyncio
async def test_verify_citations_local_laya_fails_open_on_batch_error(sample_chunks):
    """Batch inference failure/timeout → clean fail-open for all citations."""
    text = "The treatment group showed a statistically significant improvement [1]."
    with (
        patch(
            "zocrates.agents.library_search.citation_verifier._probe_local_laya_health",
            return_value=True,
        ),
        patch(
            "zocrates.agents.library_search.citation_verifier._check_support_local_laya_batch",
            return_value=None,
        ),
    ):
        results = await verify_citations_support(text, sample_chunks, config=dict(LAYA_CONFIG))
    assert results == {1: [None]}


def test_check_support_local_laya_returns_none_when_daemon_down():
    """Single-query path: connection refusal (daemon down) degrades to None."""
    with patch(
        "zocrates.agents.library_search.citation_verifier.requests.get",
        side_effect=requests.exceptions.ConnectionError("Connection refused"),
    ):
        prob = _check_support_local_laya(
            claim="Some claim",
            source_text="Some source text",
            local_url=LOCAL_URL,
        )
    assert prob is None


def test_check_support_local_laya_parses_noul_answer():
    """Healthy daemon 200 response with a numeric noul answer is clamped and returned."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"answers": {"support": {"noul": 0.87}}}
    with (
        patch(
            "zocrates.agents.library_search.citation_verifier.requests.get",
            return_value=MagicMock(status_code=200),
        ),
        patch(
            "zocrates.agents.library_search.citation_verifier.requests.post",
            return_value=mock_resp,
        ) as mock_post,
    ):
        prob = _check_support_local_laya(
            claim="Some claim",
            source_text="Some source text",
            local_url=LOCAL_URL,
        )
    assert prob == 0.87
    # Payload went to the local systemone URL with the laya model id
    assert mock_post.call_args[0][0] == LOCAL_URL
    assert mock_post.call_args[1]["json"]["model"] == "laya"