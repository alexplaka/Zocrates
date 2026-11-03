"""Unit tests for Dialogue ADK 2.* Dynamic Workflow search loop, evaluator, reranker, and tool integration."""

import json
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import ValidationError

from zocrates.agents.common.state_keys import (
    ACTIVE_CONTEXT_ITEMS,
    ACTIVE_CONTEXT_ITEMS_STR,
    HITL_SNAPSHOT,
    RETRIEVED_EVIDENCE,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEARCH_QUERY,
    TEMP_SEARCH_REPORT,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
    USER_PROMPT,
)
from zocrates.agents.coordinator.agent import (
    create_coordinator_agent,
    search_library,
    set_agent_models,
)
from zocrates.agents.common.reranker import (
    HARD_WORD_CEILING,
    MAX_RERANK_CHUNKS,
    _trim_to_budget,
    rerank_evidence,
)
from zocrates.agents.common.pipeline_base import coerce_to_text
from zocrates.agents.library_search.search_pipeline import (
    MAX_AUDIT_HISTORY_TURNS,
    _auto_generate_completion_report,
    _clean_user_facing_synthesis,
    create_library_search_agent,
    create_library_synthesizer_agent,
    search_pipeline_node,
)


# ---------------------------------------------------------------------------
# Test Context Helpers
# ---------------------------------------------------------------------------


class MockInvocationContext:
    """Mock ADK InvocationContext supporting agent reference."""

    def __init__(self, agent: Any = None):
        self.agent = agent


class MockWorkflowContext:
    """Mock ADK Context supporting state dict, invocation context, and async run_node dispatch."""

    def __init__(self, state: dict | None = None, invocation_context: Any = None):
        self.state = state if state is not None else {}
        self.run_node = AsyncMock()
        self._invocation_context = invocation_context


class MockToolContext:
    """Mock ADK ToolContext supporting actions and async run_node dispatch."""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}
        self.actions = EventActions()
        self.run_node = AsyncMock()





# ---------------------------------------------------------------------------
# 1. rerank_evidence Empty and Single
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_evidence_empty_and_single():
    """0 chunks returns empty; 1 chunk returns single item with rerank_score=None without HTTP call."""
    with patch("httpx.AsyncClient.post") as mock_post:
        # Empty
        res_empty = await rerank_evidence("query", [])
        assert res_empty == []

        # Single
        single_chunk = {
            "chunk_id": "c1",
            "text": "Only one chunk",
            "word_count": 10,
        }
        res_single = await rerank_evidence("query", [single_chunk])
        assert len(res_single) == 1
        assert res_single[0]["chunk_id"] == "c1"
        assert res_single[0]["rerank_score"] is None

        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# 6. rerank_evidence API Success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_evidence_api_success():
    """Multi-chunk candidate set is reranked, assigned rerank_score, and sorted descending."""
    chunks = [
        {"chunk_id": "c1", "text": "Low relevance chunk", "word_count": 20},
        {"chunk_id": "c2", "text": "High relevance chunk", "word_count": 20},
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.35},
            {"index": 1, "relevance_score": 0.95},
        ]
    }

    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch(
        "zocrates.agents.common.reranker.load_zocrates_config",
        return_value={"reranker_model": "rerank-model"},
    ), patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        ranked = await rerank_evidence("query", chunks)

        assert len(ranked) == 2
        # c2 should be first due to higher score 0.95
        assert ranked[0]["chunk_id"] == "c2"
        assert ranked[0]["rerank_score"] == pytest.approx(0.95)
        assert ranked[1]["chunk_id"] == "c1"
        assert ranked[1]["rerank_score"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# 7. rerank_evidence API Fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_evidence_api_fallback():
    """Network error, timeout, HTTP 500, malformed JSON, or missing key preserves original chunk order with rerank_score=None."""
    chunks = [
        {"chunk_id": "c1", "text": "Chunk 1", "word_count": 20},
        {"chunk_id": "c2", "text": "Chunk 2", "word_count": 20},
    ]

    # Sub-case A: Network error
    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Network unreachable")

        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == 2
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["rerank_score"] is None
        assert ranked[1]["chunk_id"] == "c2"
        assert ranked[1]["rerank_score"] is None

    # Sub-case B: Timeout
    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("Read timed out")

        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == 2
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["rerank_score"] is None
        assert ranked[1]["chunk_id"] == "c2"
        assert ranked[1]["rerank_score"] is None

    # Sub-case C: Non-200 HTTP status (500)
    mock_500 = MagicMock()
    mock_500.status_code = 500
    mock_500.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error", request=MagicMock(), response=mock_500
    )
    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_500

        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == 2
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["rerank_score"] is None
        assert ranked[1]["chunk_id"] == "c2"
        assert ranked[1]["rerank_score"] is None

    # Sub-case D: Malformed non-JSON body
    mock_malformed = MagicMock()
    mock_malformed.status_code = 200
    mock_malformed.json.side_effect = ValueError("Invalid JSON response")
    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_malformed

        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == 2
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["rerank_score"] is None
        assert ranked[1]["chunk_id"] == "c2"
        assert ranked[1]["rerank_score"] is None

    # Sub-case E: Missing API Key
    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key", return_value=None
    ):
        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == 2
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["rerank_score"] is None


# ---------------------------------------------------------------------------
# 8. rerank_evidence Budget Trimming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_evidence_budget_trimming():
    """Trims lowest-ranked chunks so cumulative words <= HARD_WORD_CEILING, keeping at least 1."""
    chunks = [
        {"chunk_id": "c1", "text": "A", "word_count": 4000},
        {"chunk_id": "c2", "text": "B", "word_count": 4000},
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.4},
        ]
    }

    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        ranked = await rerank_evidence("query", chunks)

        # 4000 + 4000 = 8000 > 7500, so lowest ranked (c2) must be trimmed
        assert len(ranked) == 1
        assert ranked[0]["chunk_id"] == "c1"
        assert ranked[0]["word_count"] == 4000


@pytest.mark.asyncio
async def test_rerank_evidence_caps_to_max_rerank_chunks():
    """Verifies that rerank_evidence caps to MAX_RERANK_CHUNKS (15) even if more chunks fit in budget."""
    # 25 small chunks (each 20 words = 500 words total, well under 7500 words)
    chunks = [
        {"chunk_id": f"c{i}", "text": f"Chunk {i}", "word_count": 20}
        for i in range(25)
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": i, "relevance_score": float(25 - i) / 25.0}
            for i in range(25)
        ]
    }

    with patch(
            "zocrates.agents.common.reranker.get_secure_api_key",
            return_value="test-key",
    ), patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        ranked = await rerank_evidence("query", chunks)
        assert len(ranked) == MAX_RERANK_CHUNKS
        # Highest ranked chunk (c0 with score 1.0) is present
        assert ranked[0]["chunk_id"] == "c0"
        # 16th to 25th chunks (lowest scored) were dropped
        result_ids = {c["chunk_id"] for c in ranked}
        assert "c24" not in result_ids
        assert "c15" not in result_ids

    # Verify single chunk exceeding 7500 words is still preserved
    giant_chunk = [{"chunk_id": "giant", "text": "Huge", "word_count": 9000}]
    single_trimmed = await rerank_evidence("query", giant_chunk)
    assert len(single_trimmed) == 1
    assert single_trimmed[0]["chunk_id"] == "giant"


# ---------------------------------------------------------------------------
# 9. Transient State Initialization & Query Binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_node_does_not_shadow_tool_evidence_on_entry():
    """Verify search_pipeline_node does not pre-populate empty delta containers on entry so tool writes are preserved."""
    ctx = MockWorkflowContext(state={})

    entry_state = {}

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name and not entry_state:
            entry_state["query"] = ctx.state.get(TEMP_SEARCH_QUERY)
            # Simulate tool writes
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [{"chunk_id": "new::1"}]
            ctx.state[TEMP_SEEN_CHUNK_IDS] = ["new::1"]
            ctx.state[TEMP_SEEN_ASSETS] = ["new:s1"]
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [{"chunk_id": "new::1"}]

        await search_pipeline_node._func(ctx, "fresh research query")

    # Verify query was bound and tool writes were preserved
    assert entry_state["query"] == "fresh research query"
    assert len(ctx.state[TEMP_RETRIEVED_EVIDENCE]) == 1


@pytest.mark.asyncio
async def test_search_pipeline_node_query_binding():
    """Verify search_pipeline_node._func sets active search query in state."""
    ctx = MockWorkflowContext(state={
        TEMP_RETRIEVED_EVIDENCE: [],
        TEMP_SEEN_CHUNK_IDS: [],
        TEMP_SEEN_ASSETS: [],
    })

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            assert ctx.state[TEMP_SEARCH_QUERY] == "fresh research query"
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "fresh::1", "text": "Fresh text", "word_count": 20}
            ]
            return "searched"
        elif "synthesizer" in node_name:
            return "Synthesized answer"
        return "ok"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"chunk_id": "fresh::1", "text": "Fresh text", "word_count": 20}
        ]

        result = await search_pipeline_node._func(ctx, "fresh research query")

        assert result == "Synthesized answer"
        assert ctx.state[TEMP_SEARCH_QUERY] == "fresh research query"


# ---------------------------------------------------------------------------
# 10. search_pipeline_node Loop Termination on Pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_node_loop_termination_pass():
    """Simulates evidence passing sufficiency on turn 1 -> breaks early after 1 iteration."""
    ctx = MockWorkflowContext(state={})
    turns_executed = 0

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal turns_executed
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            turns_executed += 1
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Pass text", "word_count": 100}
            ]
            return "done"
        return "Synthesized result"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "Pass text", "word_count": 100}
        ]

        await search_pipeline_node._func(ctx, "mutation rates")

        assert turns_executed == 1


# ---------------------------------------------------------------------------
# 11. search_pipeline_node Loop Termination on Max Turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_node_single_pass_execution():
    """Verifies that search_subagent executes in a single pass without an outer turn loop."""
    ctx = MockWorkflowContext(state={})
    turns_executed = 0

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal turns_executed
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            turns_executed += 1
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": f"c{turns_executed}", "text": "Text", "word_count": 50}
            ]
            return "done"
        return "Final answer"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "hard question")

        assert turns_executed == 1


# ---------------------------------------------------------------------------
# 12. search_pipeline_node Audit Logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_node_audit_logging():
    """Verifies that upon completion, a turn audit record is appended to ctx.state[RETRIEVED_EVIDENCE]."""
    ctx = MockWorkflowContext(
        state={USER_PROMPT: "Explain somatic mutations", RETRIEVED_EVIDENCE: []}
    )

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Findings", "word_count": 100}
            ]
            return "done"
        return "Synthesized explanation"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "Findings", "word_count": 100}
        ]

        await search_pipeline_node._func(ctx, "somatic mutations in twins")

        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == 1
        entry = audit_history[0]
        assert entry["user_query"] == "Explain somatic mutations"
        assert entry["search_query"] == "somatic mutations in twins"
        assert len(entry["evidence_chunks"]) == 1
        assert entry["cited_chunk_ids"] == []
        assert entry["enriched_text"] == "Synthesized explanation"


@pytest.mark.asyncio
async def test_search_pipeline_node_resolution_failure_sets_enriched_text_none():
    """Verifies that if citation resolution throws an exception, enriched_text is set to None in audit log."""
    ctx = MockWorkflowContext(
        state={
            USER_PROMPT: "Explain somatic mutations",
            RETRIEVED_EVIDENCE: [],
        }
    )

    async def mock_run_node(agent, *args, **kwargs):
        if "search" in getattr(agent, "name", ""):
            return None
        return "Synthesized explanation with [1]"

    ctx.run_node.side_effect = mock_run_node

    with (
        patch(
            "zocrates.agents.library_search.citation_verifier.verify_citations_support",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "zocrates.agents.library_search.search_pipeline.rerank_evidence",
            new_callable=AsyncMock,
        ) as mock_rerank,
        patch(
            "zocrates.agents.library_search.citation_resolver.resolve_citations_and_references",
            side_effect=RuntimeError("Resolution crash"),
        ),
    ):
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "Findings", "word_count": 100, "item_key": "K1"}
        ]

        await search_pipeline_node._func(ctx, "somatic mutations in twins")

        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == 1
        entry = audit_history[0]
        assert entry["enriched_text"] is None


@pytest.mark.asyncio
async def test_search_pipeline_node_audit_dedup_on_resume():
    """Verifies that resuming or retrying the same query updates the last audit entry rather than creating duplicates."""
    initial_audit = [
        {
            "user_query": "Explain somatic mutations",
            "search_query": "somatic mutations in twins",
            "evidence_chunks": [{"chunk_id": "c0", "text": "Old findings"}],
            "cited_chunk_ids": [],
        }
    ]
    ctx = MockWorkflowContext(
        state={
            USER_PROMPT: "Explain somatic mutations",
            RETRIEVED_EVIDENCE: list(initial_audit),
        }
    )

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "New findings", "word_count": 100}
            ]
            return "done"
        return "Synthesized explanation"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "New findings", "word_count": 100}
        ]

        await search_pipeline_node._func(ctx, "somatic mutations in twins")

        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == 1  # Deduplicated / updated in place
        assert audit_history[0]["evidence_chunks"][0]["chunk_id"] == "c1"


@pytest.mark.asyncio
async def test_search_pipeline_node_audit_cap_max_turns():
    """Verifies that persistent audit history is capped to MAX_AUDIT_HISTORY_TURNS (5)."""
    # Pre-populate with 5 older turns
    older_history = [
        {
            "user_query": f"Query {i}",
            "search_query": f"Search {i}",
            "evidence_chunks": [],
            "cited_chunk_ids": [],
        }
        for i in range(MAX_AUDIT_HISTORY_TURNS)
    ]
    ctx = MockWorkflowContext(
        state={
            USER_PROMPT: "Fresh query",
            RETRIEVED_EVIDENCE: list(older_history),
        }
    )

    async def mock_run_node(node_callable, *args, **kwargs):
        return "Answer"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "Fresh search query")

        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == MAX_AUDIT_HISTORY_TURNS
        assert audit_history[0]["search_query"] == "Search 1"  # Oldest (Search 0) evicted
        assert audit_history[-1]["search_query"] == "Fresh search query"


@pytest.mark.asyncio
async def test_search_pipeline_passes_evidence_to_reranker():
    """Verifies that accumulated evidence is passed to rerank_evidence."""
    ctx = MockWorkflowContext(state={})
    evidence_at_rerank = None

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Evidence snippet", "word_count": 20}
            ]
            return "done"
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    async def mock_rerank_fn(query, evidence, *args, **kwargs):
        nonlocal evidence_at_rerank
        evidence_at_rerank = evidence
        return evidence

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        side_effect=mock_rerank_fn,
    ):
        await search_pipeline_node._func(ctx, "test search")

        assert evidence_at_rerank is not None
        assert evidence_at_rerank[0]["chunk_id"] == "c1"


@pytest.mark.asyncio
async def test_search_pipeline_passes_active_selection_in_node_input():
    """Verifies that active Zotero selection is passed dynamically via node_input to search and synth agents."""
    ctx = MockWorkflowContext(
        state={
            ACTIVE_CONTEXT_ITEMS: [
                {"item_key": "ABC12345", "title": "Test Paper", "year": "2024"}
            ],
            ACTIVE_CONTEXT_ITEMS_STR: '- [Key: ABC12345] "Test Paper" (2024)',
            RETRIEVED_EVIDENCE: [],
        }
    )

    received_inputs = {}

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            received_inputs["search"] = kwargs.get("node_input")
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Paper findings", "word_count": 20}
            ]
            return "done"
        elif "synthesizer" in node_name:
            received_inputs["synth"] = kwargs.get("node_input")
            return "Final synthesis"
        return "ok"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "Paper findings", "word_count": 20}
        ]

        await search_pipeline_node._func(ctx, "mutation query")

    assert "Context: Active Items Selected in Zotero" in received_inputs["search"]
    assert "ABC12345" in received_inputs["search"]
    assert "mutation query" in received_inputs["search"]

    assert "Context: Active Items Selected in Zotero" in received_inputs["synth"]
    assert "ABC12345" not in received_inputs["synth"]
    assert "Test Paper" in received_inputs["synth"]
    assert "Retrieved Literature Evidence" in received_inputs["synth"]


@pytest.mark.asyncio
async def test_search_pipeline_single_pass_search_node_input():
    """Verifies the single-pass search passes the clean search goal to search_subagent."""
    ctx = MockWorkflowContext(state={})
    node_inputs = []

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            node_inputs.append(kwargs.get("node_input"))
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Text", "word_count": 50}
            ]
            return "done"
        return "Synthesized result"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "mutation query")

    assert len(node_inputs) == 1
    assert node_inputs[0] == "# Target Search Goal\nmutation query"


# ---------------------------------------------------------------------------
# 13. search_pipeline_node Output Coercion
# ---------------------------------------------------------------------------


def test_search_pipeline_node_output_coercion():
    """Verify coerce_to_text extracts plain text from str, types.Content, dict, and object."""
    assert coerce_to_text("Simple string") == "Simple string"

    content = types.Content(
        role="model", parts=[types.Part(text="Extracted text from Content")]
    )
    assert coerce_to_text(content) == "Extracted text from Content"

    dict_obj = {"text": "From dict key"}
    assert coerce_to_text(dict_obj) == "From dict key"

    class ResponseObj:
        text = "From attribute text"

    assert coerce_to_text(ResponseObj()) == "From attribute text"


# ---------------------------------------------------------------------------
# 14. library_search FunctionTool Integration
# ---------------------------------------------------------------------------


def test_create_search_subagent_uses_default_include_contents():
    """Search subagent explicitly enables full pipeline history via include_contents='default'."""
    agent = create_library_search_agent("test-model")
    assert agent.mode == "single_turn"
    assert agent.include_contents == "default"


def test_create_search_subagent_empty_include_contents_not_none():
    """Search subagent does not use the deprecated 'none' include_contents mode."""
    agent = create_library_search_agent("test-model")
    assert agent.include_contents != "none"


@pytest.mark.asyncio
async def test_library_search_tool():
    """Calls search_library: sets skip_summarization=True and dispatches via run_node."""
    tool_ctx = MockToolContext()
    tool_ctx.run_node.return_value = "Grounded synthesis response"

    res = await search_library("twin genetics", tool_ctx)

    assert res == "Grounded synthesis response"
    assert tool_ctx.actions.skip_summarization is True
    tool_ctx.run_node.assert_called_once_with(
        search_pipeline_node,
        node_input="twin genetics",
        override_isolation_scope="search_library_pipeline:default",
    )


@pytest.mark.asyncio
async def test_library_search_passes_function_call_id_isolation_scope():
    """search_library derives the search-pipeline isolation scope from the tool function_call_id."""
    tool_ctx = MockToolContext()
    tool_ctx.function_call_id = "fc-7c1e"
    tool_ctx.run_node.return_value = "result"

    await search_library("twin genetics", tool_ctx)

    tool_ctx.run_node.assert_called_once_with(
        search_pipeline_node,
        node_input="twin genetics",
        override_isolation_scope="search_library_pipeline:fc-7c1e",
    )


@pytest.mark.asyncio
async def test_library_search_falls_back_to_invocation_id_isolation_scope():
    """search_library falls back to invocation_id when function_call_id is absent."""
    tool_ctx = MockToolContext()
    tool_ctx.invocation_id = "inv-42"
    tool_ctx.run_node.return_value = "result"

    await search_library("twin genetics", tool_ctx)

    tool_ctx.run_node.assert_called_once_with(
        search_pipeline_node,
        node_input="twin genetics",
        override_isolation_scope="search_library_pipeline:inv-42",
    )


# ---------------------------------------------------------------------------
# 15. set_agent_models Propagation
# ---------------------------------------------------------------------------


def test_set_agent_models():
    """Verifies that set_agent_models updates models on coordinator and all subagents."""
    coordinator = create_coordinator_agent("initial-model")

    assert coordinator.model == "initial-model"
    assert coordinator._subagents["library_search"].model == "initial-model"
    assert coordinator._subagents["library_synthesizer"].model == "initial-model"

    set_agent_models(coordinator, "updated-model")

    assert coordinator.model == "updated-model"
    assert coordinator._subagents["library_search"].model == "updated-model"
    assert coordinator._subagents["library_synthesizer"].model == "updated-model"


def test_set_agent_models_with_dict():
    """Verifies that set_agent_models sets distinct models per agent when given a dict."""
    coordinator = create_coordinator_agent("initial-model")

    set_agent_models(
        coordinator,
        {
            "coordinator": "coord-model",
            "library_search": "search-model",
            "library_synthesizer": "synth-model",
        },
    )

    assert coordinator.model == "coord-model"
    assert coordinator._subagents["library_search"].model == "search-model"
    assert coordinator._subagents["library_synthesizer"].model == "synth-model"


def test_set_agent_models_with_canonical_keys():
    """Verifies that canonical role keys work in dict model spec."""
    coordinator = create_coordinator_agent("initial-model")

    set_agent_models(
        coordinator,
        {
            "coordinator": "coord-2",
            "library_search": "search-2",
            "library_synthesizer": "synth-2",
        },
    )

    assert coordinator.model == "coord-2"
    assert coordinator._subagents["library_search"].model == "search-2"
    assert coordinator._subagents["library_synthesizer"].model == "synth-2"


def test_set_agent_models_with_dict_default_fallback():
    """Verifies that 'default' key in model dict applies to unspecified agents."""
    coordinator = create_coordinator_agent("initial-model")

    set_agent_models(
        coordinator,
        {
            "default": "base-model",
            "library_search": "fast-model",
        },
    )

    assert coordinator.model == "base-model"
    assert coordinator._subagents["library_search"].model == "fast-model"
    assert coordinator._subagents["library_synthesizer"].model == "base-model"


def test_create_coordinator_agent_with_dict():
    """Verifies that create_coordinator_agent initializes per-agent models when passed a dict."""
    coordinator = create_coordinator_agent(
        model={
            "default": "base-model",
            "library_search": "search-model",
        }
    )

    assert coordinator.model == "base-model"
    assert coordinator._subagents["library_search"].model == "search-model"
    assert coordinator._subagents["library_synthesizer"].model == "base-model"


@pytest.mark.asyncio
async def test_search_pipeline_resolves_subagents_from_coordinator():
    """Verify search_pipeline_node._func pulls subagents from parent coordinator's _subagents."""
    custom_search = create_library_search_agent("custom-model")
    custom_synth = create_library_synthesizer_agent("custom-model")

    mock_coordinator = Agent(name="test_coordinator", instruction="Test")
    mock_coordinator._subagents = {
        "library_search": custom_search,
        "library_synthesizer": custom_synth,
    }

    ctx = MockWorkflowContext(
        state={RETRIEVED_EVIDENCE: []},
        invocation_context=MockInvocationContext(agent=mock_coordinator),
    )

    executed_agents = []

    async def mock_run_node(node_callable, *args, **kwargs):
        executed_agents.append(node_callable)
        return "Synthesized text"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "twin studies")

        # Verify that custom subagents from coordinator were used
        assert custom_search in executed_agents
        assert custom_synth in executed_agents


@pytest.mark.asyncio
async def test_search_pipeline_node_closes_isolation_scope_on_completion():
    """search_pipeline_node enqueues a terminal finish_task event closing its isolation scope.

    Verifies the fix for multi-turn session poisoning: without an explicit
    closing finish_task event, ADK's Runner treats ``search_pipeline:{fc_id}`` as
    an unclosed in-flight task agent and tags subsequent user turns with the
    stale scope, causing the coordinator (isolation_scope=None) to drop them.
    """
    from types import SimpleNamespace

    class RecordingInvocationContext:
        def __init__(self):
            self.agent = SimpleNamespace(
                _subagents={"search": object(), "synthesizer": object()}
            )
            self.enqueued_events = []

        async def _enqueue_event(self, event):
            self.enqueued_events.append(event)

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={RETRIEVED_EVIDENCE: []},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_pipeline:call-7c1e"
    ctx.agent_name = "search_pipeline_node"

    async def mock_run_node(node_callable, *args, **kwargs):
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        result = await search_pipeline_node._func(ctx, "twin studies")

    assert result == "Synthesized answer"
    assert len(invocation_ctx.enqueued_events) == 1
    closing = invocation_ctx.enqueued_events[0]
    assert closing.author == "search_pipeline_node"
    assert closing.isolation_scope == "search_pipeline:call-7c1e"
    assert closing.content.role == "tool"
    fr = closing.content.parts[0].function_response
    assert fr.name == "finish_task"
    assert fr.response == {"result": "Task completed."}


@pytest.mark.asyncio
async def test_search_pipeline_node_emits_task_failed_on_exception():
    """search_pipeline_node still closes its isolation scope with Task failed. on exceptions."""
    from types import SimpleNamespace

    class RecordingInvocationContext:
        def __init__(self):
            self.agent = SimpleNamespace(
                _subagents={"search": object(), "synthesizer": object()}
            )
            self.enqueued_events = []

        async def _enqueue_event(self, event):
            self.enqueued_events.append(event)

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={RETRIEVED_EVIDENCE: []},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_pipeline:call-7c1e"
    ctx.agent_name = "search_pipeline_node"

    async def mock_run_node(node_callable, *args, **kwargs):
        raise RuntimeError("synthetic pipeline failure")

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []
        with pytest.raises(RuntimeError):
            await search_pipeline_node._func(ctx, "twin studies")

    # The finally block must still emit the closing event, marked as failed.
    assert len(invocation_ctx.enqueued_events) == 1
    closing = invocation_ctx.enqueued_events[0]
    assert closing.isolation_scope == "search_pipeline:call-7c1e"
    assert closing.content.role == "tool"
    fr = closing.content.parts[0].function_response
    assert fr.name == "finish_task"
    assert fr.response == {"result": "Task failed."}


# ---------------------------------------------------------------------------
# 16. InMemoryRunner End-to-End Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_in_memory_runner():
    """End-to-end test of coordinator with library_search using real InMemoryRunner.

    Verifies parameter binding, tool_context.run_node execution, output extraction,
    and session state audit log persistence.
    """

    class MockCoordinatorAgent(Agent):
        async def _run_async_impl(self, ctx):
            tool = self.tools[0]
            tool_ctx = ToolContext(ctx)
            res = await tool.run_async(
                args={"request": "twin somatic mutations"},
                tool_context=tool_ctx,
            )
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=str(res))],
                ),
            )

    search_tool = FunctionTool(func=search_library)
    coord = MockCoordinatorAgent(
        name="test_coordinator", instruction="Test", tools=[search_tool]
    )
    app = App(name="test_dialogue_app", root_agent=coord)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="test_dialogue_app", user_id="test_user"
    )

    with patch(
            "zocrates.agents.library_search.search_pipeline.create_library_search_agent"
    ) as mock_sub, patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank, patch(
        "zocrates.agents.library_search.search_pipeline.create_library_synthesizer_agent"
    ) as mock_synth:
        def mock_search_fn(ctx, node_input: str = ""):
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {
                    "chunk_id": "c1",
                    "text": "Twin mutation findings",
                    "word_count": 25,
                }
            ]
            return "search_done"

        def mock_synth_fn(ctx, node_input: str = ""):
            return "Synthesized grounded answer on twins"

        mock_sub.return_value = mock_search_fn
        mock_rerank.return_value = [
            {
                "chunk_id": "c1",
                "text": "Twin mutation findings",
                "word_count": 25,
            }
        ]
        mock_synth.return_value = mock_synth_fn

        user_msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text="What are twin mutation rates?")],
        )
        events = []
        async for event in runner.run_async(
                user_id="test_user", session_id=session.id, new_message=user_msg
        ):
            events.append(event)

        texts = [
            e.content.parts[0].text
            for e in events
            if e.content and e.content.parts
        ]
        assert len(texts) >= 1
        assert "Synthesized grounded answer on twins" in texts[-1]

        # Verify audit history persisted in session state
        updated_session = await runner.session_service.get_session(
            app_name="test_dialogue_app",
            user_id="test_user",
            session_id=session.id,
        )
        assert updated_session is not None
        audit_history = updated_session.state.get(RETRIEVED_EVIDENCE, [])
        assert len(audit_history) == 1
        assert audit_history[0]["search_query"] == "twin somatic mutations"
        assert len(audit_history[0]["evidence_chunks"]) == 1


@pytest.mark.asyncio
async def test_search_pipeline_multi_turn_scope_closure_prevents_user_message_hijacking():
    """End-to-end multi-turn test: closing finish_task event prevents Turn 2 hijacking.

    Verifies the multi-turn session poisoning fix against the real ADK Runner:
    - Turn 1 runs with coordinator -> library_search -> search_pipeline_node.
    - After Turn 1 completes, ``_find_active_task_scope(session)`` returns None
      (the pipeline's isolation scope was explicitly closed).
    - Turn 2 runs with a new user message.
    - The Turn 2 user message event in ``session.events`` has
      ``isolation_scope=None`` (not hijacked by Turn 1's search_pipeline scope).
    """
    from google.adk.models.base_llm import BaseLlm
    from google.adk.runners import Runner, _find_active_task_scope
    from google.adk.sessions import InMemorySessionService

    turn_state = {"calls": 0}
    received_contents: list[list[types.Content]] = []

    class MockTurnModel(BaseLlm):
        model: str = "mock"

        async def generate_content_async(self, req, stream=False):
            received_contents.append(list(req.contents))
            turn_state["calls"] += 1
            call_num = turn_state["calls"]
            if call_num == 1:
                # Turn 1: emit the library_search tool call for the pipeline.
                part = types.Part.from_function_call(
                    name="search_library",
                    args={"request": "twin somatic mutations"},
                )
            else:
                # The pipeline result is streamed directly (skip_summarization),
                # so turns 2+ answer immediately without re-summarizing.
                part = types.Part.from_text(text=f"Answer {call_num - 1}")
            yield LlmResponse(
                content=types.Content(role="model", parts=[part])
            )

    coordinator = Agent(
        name="coordinator_agent",
        model=MockTurnModel(),
        instruction="Test coordinator",
        tools=[FunctionTool(func=search_library)],
        mode="chat",
    )
    app = App(name="test_multi_turn_scope_app", root_agent=coordinator)
    session_service = InMemorySessionService()
    runner = Runner(
        app=app,
        session_service=session_service,
        auto_create_session=True,
    )

    with patch(
        "zocrates.agents.library_search.search_pipeline.create_library_search_agent"
    ) as mock_sub, patch(
        "zocrates.agents.library_search.search_pipeline.create_library_synthesizer_agent"
    ) as mock_synth, patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        def mock_search_fn(ctx, node_input: str = ""):
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {
                    "chunk_id": "c1",
                    "text": "Twin mutation findings",
                    "word_count": 25,
                }
            ]
            return "search_done"

        def mock_synth_fn(ctx, node_input: str = ""):
            return "Synthesized grounded answer"

        mock_sub.return_value = mock_search_fn
        mock_synth.return_value = mock_synth_fn
        mock_rerank.return_value = []

        # Turn 1: coordinator -> library_search -> search_pipeline_node
        async for _ in runner.run_async(
            user_id="test_user",
            session_id="multi_turn_s1",
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Question one")],
            ),
        ):
            pass

        session = await session_service.get_session(
            app_name="test_multi_turn_scope_app",
            user_id="test_user",
            session_id="multi_turn_s1",
        )
        assert session is not None
        # The pipeline's isolation scope was closed: ADK must not treat it as
        # an in-flight task agent awaiting user input.
        assert _find_active_task_scope(session) is None

        # Turn 2: new user message must flow to the coordinator normally.
        async for _ in runner.run_async(
            user_id="test_user",
            session_id="multi_turn_s1",
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Question two")],
            ),
        ):
            pass

        session = await session_service.get_session(
            app_name="test_multi_turn_scope_app",
            user_id="test_user",
            session_id="multi_turn_s1",
        )
        assert session is not None
        # The Turn 2 user message must not be hijacked by Turn 1's scope.
        user_text_events = [
            e
            for e in session.events
            if e.author == "user"
            and e.content is not None
            and any(p.text for p in e.content.parts)
        ]
        assert len(user_text_events) == 2
        turn2_event = user_text_events[-1]
        assert turn2_event.isolation_scope is None

        # And the coordinator must have been able to answer Turn 2 (i.e., it
        # received the second user message rather than replaying Turn 1). With
        # skip_summarization the tool result is streamed directly, so exactly
        # one LLM round runs per turn (Turn 1 emits the tool call; Turn 2
        # answers the new question).
        assert turn_state["calls"] == 2
        assert len(received_contents) == 2
        last_round_texts = [
            p.text
            for content in received_contents[-1]
            for p in content.parts
            if p.text
        ]
        test_query_texts = [t for t in last_round_texts if "Question" in t]
        assert test_query_texts == [
            "Question one",
            "Question two",
        ], f"Coordinator LLM call {received_contents[-1]}"


# ---------------------------------------------------------------------------
# 17. Thinking Configuration Defaults and Tuning
# ---------------------------------------------------------------------------


def test_dialogue_agents_thinking_config_default_and_tuning():
    """Verifies that agents configure role-specific thinking defaults (coordinator=medium, search=low, synth=high) and support tuning."""
    coordinator = create_coordinator_agent("test-model")

    expected_role_levels = {
        "coordinator": (coordinator, types.ThinkingLevel.MEDIUM, "medium"),
        "library_search": (coordinator._subagents["library_search"], types.ThinkingLevel.LOW, "low"),
        "library_synthesizer": (coordinator._subagents["library_synthesizer"], types.ThinkingLevel.HIGH, "high"),
    }

    for role_name, (agent_obj, expected_enum, expected_str) in expected_role_levels.items():
        cfg = agent_obj.generate_content_config
        assert cfg is not None, f"Expected config for {role_name}"
        assert cfg.thinking_config is not None, f"Expected thinking_config for {role_name}"
        assert cfg.thinking_config.thinking_level == expected_enum, f"Mismatch for {role_name}"
        assert cfg.thinking_config.include_thoughts is True
        assert cfg.http_options is not None
        assert cfg.http_options.extra_body["reasoning"]["effort"] == expected_str, f"Mismatch effort for {role_name}"

    # Test tuning coordinator to 'low'
    low_coord = create_coordinator_agent("test-model", thinking_level="low")
    assert low_coord.generate_content_config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert low_coord.generate_content_config.http_options.extra_body["reasoning"]["effort"] == "low"

    # Test disabling thinking
    none_coord = create_coordinator_agent("test-model", thinking_level=None)
    assert none_coord.generate_content_config.thinking_config is None


# ---------------------------------------------------------------------------
# 18. Termination Tool & Search Iteration Report Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_node_auto_generates_report_if_uncalled():
    """Verifies that an iteration report is automatically synthesized when the subagent doesn't call conclude_search."""
    ctx = MockWorkflowContext(
        state={USER_PROMPT: "Explain coding loop", RETRIEVED_EVIDENCE: []}
    )

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {
                    "chunk_id": "c1",
                    "heading": "Loop Architecture",
                    "chunk_type": "text",
                    "text": "Continuous loop",
                    "word_count": 50,
                }
            ]
            # Subagent yields without setting TEMP_SEARCH_REPORT
            return "done"
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "Explain coding loop")

        # Report must have been automatically synthesized
        report = ctx.state.get(TEMP_SEARCH_REPORT)
        assert report is not None
        assert report["source"] == "auto_generated"
        assert "Loop Architecture" in report["summary"]


# ---------------------------------------------------------------------------
# 19. User-Facing Cleanliness Tests
# ---------------------------------------------------------------------------


def test_clean_user_facing_synthesis():
    """Verifies that internal keys, hashes, and chunk references are deterministically stripped."""
    raw = (
        '## Self-Improving Coding Agent Framework (HCZLU22L: "Self-Improving Coding Agents")\n\n'
        'In Chunk HCZLU22L:s4, the authors explain the continuous coding loop.\n'
        'Furthermore, [Chunk: HCZLU22L::2] details benchmark evaluations.\n'
        'Refer to [Key: HCZLU22L] for details.'
    )
    cleaned = _clean_user_facing_synthesis(raw)
    assert 'HCZLU22L' not in cleaned
    assert 'Chunk' not in cleaned
    assert 'Key:' not in cleaned
    assert '("Self-Improving Coding Agents")' in cleaned
    assert 'the authors explain the continuous coding loop.' in cleaned


def test_clean_user_facing_synthesis_strips_attachment_and_note_keys():
    """Verifies [Attachment Key: ...] and [Note Key: ...] are stripped from synthesized output."""
    raw = (
        "See [Attachment Key: 3A4B5C6D] for the main paper and "
        "[Note Key: 9X8Y7Z6W] for the reading notes. "
        "Also check [Key: ABCD1234] directly."
    )
    cleaned = _clean_user_facing_synthesis(raw)
    assert "[Attachment Key:" not in cleaned
    assert "[Note Key:" not in cleaned
    assert "[Key:" not in cleaned
    assert "3A4B5C6D" not in cleaned
    assert "9X8Y7Z6W" not in cleaned
    assert "ABCD1234" not in cleaned
    # Surrounding prose must survive
    assert "for the main paper" in cleaned
    assert "for the reading notes" in cleaned
    assert "Also check" in cleaned


def test_clean_user_facing_synthesis_strips_thinking_tags():
    """Verifies that closed, unclosed, and pre-injected <think> tags are thoroughly stripped."""
    from zocrates.agents.online_search.search_pipeline import (
        _clean_user_facing_synthesis as online_clean,
    )

    # 1. Closed thinking block
    raw_closed = (
        "<think>\nNeed to cite paper [1] and discuss method.\n</think>\n\n"
        "This paper introduces Medusa speculative decoding."
    )
    cleaned_lib = _clean_user_facing_synthesis(raw_closed)
    assert "<think>" not in cleaned_lib
    assert "</think>" not in cleaned_lib
    assert "Need to cite paper" not in cleaned_lib
    assert "This paper introduces Medusa speculative decoding." in cleaned_lib

    cleaned_onl = online_clean(raw_closed)
    assert "<think>" not in cleaned_onl
    assert "</think>" not in cleaned_onl
    assert "Need to cite paper" not in cleaned_onl
    assert "This paper introduces Medusa speculative decoding." in cleaned_onl

    # 2. Unclosed thinking block (from token truncation)
    raw_unclosed = (
        "<think>\nI am planning the synthesis step by step...\n"
        "Drafting internal thoughts that ran out of tokens"
    )
    assert _clean_user_facing_synthesis(raw_unclosed) == ""
    assert online_clean(raw_unclosed) == ""

    # 3. Pre-injected / orphaned closing tag
    raw_orphaned = (
        "Internal scratchpad remnants before the closing tag.</think>\n\n"
        "Actual synthesis starts here."
    )
    cleaned_orph = _clean_user_facing_synthesis(raw_orphaned)
    assert "</think>" not in cleaned_orph
    assert "Internal scratchpad remnants" not in cleaned_orph
    assert "Actual synthesis starts here." in cleaned_orph

    cleaned_onl_orph = online_clean(raw_orphaned)
    assert "</think>" not in cleaned_onl_orph
    assert "Internal scratchpad remnants" not in cleaned_onl_orph
    assert "Actual synthesis starts here." in cleaned_onl_orph

    # 4. Multi-block thinking tags
    raw_multi = (
        "<think>Initial plan</think>First section.\n"
        "<think>Second plan</think>Second section."
    )
    cleaned_multi = _clean_user_facing_synthesis(raw_multi)
    assert "<think>" not in cleaned_multi
    assert "Initial plan" not in cleaned_multi
    assert "Second plan" not in cleaned_multi
    assert "First section." in cleaned_multi
    assert "Second section." in cleaned_multi

    # 5. Code block protection: <think> inside backticks or code blocks must NOT be stripped
    raw_code_inline = "The authors examine the `<think>` prompt tag. Specifically, they find a 15% boost."
    cleaned_inline = _clean_user_facing_synthesis(raw_code_inline)
    assert "`<think>`" in cleaned_inline
    assert "15% boost" in cleaned_inline

    raw_fenced = "```xml\n<think>\nthis is code\n</think>\n```\nExplanation text."
    cleaned_fenced = _clean_user_facing_synthesis(raw_fenced)
    assert "<think>" in cleaned_fenced
    assert "</think>" in cleaned_fenced
    assert "Explanation text." in cleaned_fenced

    # 6. Mid-document unclosed <think> without backticks must NOT truncate the remaining text
    raw_mid_prose = "In this section, we compare <think> with other tokens. Results show high accuracy."
    cleaned_mid = _clean_user_facing_synthesis(raw_mid_prose)
    assert "Results show high accuracy." in cleaned_mid


@pytest.mark.asyncio
async def test_search_pipeline_node_sanitizes_active_items_for_synthesizer():
    """Verifies search_pipeline_node strips [Key: ...] from Active Zotero Selection before synthesizer."""
    ctx = MockWorkflowContext(
        state={
            USER_PROMPT: "Explain coding loop",
            ACTIVE_CONTEXT_ITEMS_STR: 'Active context items selected in Zotero:\n- [Key: HCZLU22L] Title: "Self-Improving Coding Agents" (2025)',
            RETRIEVED_EVIDENCE: [],
        }
    )

    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {
                    "chunk_id": "c1",
                    "title": "Self-Improving Coding Agents",
                    "heading": "Loop",
                    "chunk_type": "text",
                    "text": "Continuous loop",
                    "word_count": 50,
                }
            ]
            return "done"
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "Synthesis without keys."
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "Explain coding loop")

        assert received_synth_prompt is not None
        assert "[Key: HCZLU22L]" not in received_synth_prompt
        assert 'Title: "Self-Improving Coding Agents"' in received_synth_prompt
        # Target search goal must not be swallowed by active context formatting
        assert "# Target Search Goal\nExplain coding loop" in received_synth_prompt
        assert "# Context: Active Items Selected in Zotero" in received_synth_prompt
        assert "# Retrieved Literature Evidence" in received_synth_prompt


@pytest.mark.asyncio
async def test_search_pipeline_synth_prompt_construction_without_active_items():
    """Verify synth_prompt independently preserves User Query, Search Goal, and Evidence when active items are empty."""
    ctx = MockWorkflowContext(state={})
    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "Clean synthesis response."
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"title": "Doc A", "text": "Evidence text A", "word_count": 50, "chunk_type": "text"}
        ]

        await search_pipeline_node._func(ctx, "Independent query without active items")

        assert received_synth_prompt is not None
        assert "# User Query\nIndependent query without active items" in received_synth_prompt
        assert "# Context: Active Items Selected in Zotero" not in received_synth_prompt
        assert "# Target Search Goal\nIndependent query without active items" in received_synth_prompt
        assert "# Retrieved Literature Evidence" in received_synth_prompt
        assert "Doc A" in received_synth_prompt


@pytest.mark.asyncio
async def test_search_pipeline_preserves_distinct_user_prompt_and_target_goal():
    """Verify that user_prompt and search_query remain distinct in the synthesizer prompt and audit log."""
    ctx = MockWorkflowContext(state={USER_PROMPT: "Explain somatic mutations in twins"})
    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "Synthesized answer."
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"title": "Jonsson 2021", "text": "Evidence text", "word_count": 50, "chunk_type": "text"}
        ]

        await search_pipeline_node._func(
            ctx,
            "Jonsson et al. 2021 somatic mutation rates monozygotic twins",
        )

        assert received_synth_prompt is not None
        assert "# User Query\nExplain somatic mutations in twins" in received_synth_prompt
        assert (
            "# Target Search Goal\nJonsson et al. 2021 somatic mutation rates monozygotic twins"
            in received_synth_prompt
        )

        audit = ctx.state.get(RETRIEVED_EVIDENCE)
        assert audit is not None and len(audit) > 0
        assert audit[-1]["user_query"] == "Explain somatic mutations in twins"
        assert (
            audit[-1]["search_query"]
            == "Jonsson et al. 2021 somatic mutation rates monozygotic twins"
        )


@pytest.mark.asyncio
async def test_search_pipeline_resume_recovers_search_query_from_snapshot():
    """Verify that on HITL resume, search_query is recovered from snapshot rather than clobbered by node_input."""
    coordinator_goal = "Jonsson et al. 2021 somatic mutation rates monozygotic twins"
    user_raw_query = "What about their mutation rates in twins?"

    snapshot = {
        "search_query": coordinator_goal,
        "evidence": [{"title": "Doc A", "text": "Evidence A", "word_count": 40, "chunk_type": "text"}],
        "search_report": {"summary": "Preliminary", "gaps": []},
        "extension_count": 1,
        "completed_extensions": [1],
    }

    ctx = MockWorkflowContext(state={HITL_SNAPSHOT: snapshot, USER_PROMPT: user_raw_query})
    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "Synthesized answer."
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = snapshot["evidence"]

        # ADK's _run_node_async passes the user message as node_input on resume
        await search_pipeline_node._func(ctx, user_raw_query)

        assert received_synth_prompt is not None
        # User query must match user_raw_query
        assert f"# User Query\n{user_raw_query}" in received_synth_prompt
        # Target search goal must NOT be clobbered by node_input; it must be the coordinator_goal from snapshot
        assert f"# Target Search Goal\n{coordinator_goal}" in received_synth_prompt

        audit = ctx.state.get(RETRIEVED_EVIDENCE)
        assert audit is not None and len(audit) > 0
        assert audit[-1]["user_query"] == user_raw_query
        assert audit[-1]["search_query"] == coordinator_goal


@pytest.mark.asyncio
async def test_search_pipeline_user_query_falls_back_to_session_events():
    """Verify that when state[USER_PROMPT] is missing, user_query is extracted from session.events

    following ADK's forward invocation_id-scoped contract (no prior turn bleed, no later message clobbering).
    """
    class MockEventPart:
        def __init__(self, text: str, thought: bool = False):
            self.text = text
            self.thought = thought

    class MockEventContent:
        def __init__(self, parts: list):
            self.parts = parts

    class MockEvent:
        def __init__(self, author: str, content: Any, invocation_id: str = None):
            self.author = author
            self.content = content
            self.invocation_id = invocation_id

    class MockSession:
        def __init__(self, events: list):
            self.events = events

    prior_event = MockEvent(
        author="user",
        content=MockEventContent([MockEventPart("Old query from previous turn")]),
        invocation_id="prior_inv",
    )
    initial_user_event = MockEvent(
        author="user",
        content=MockEventContent([MockEventPart("How do transformers work?")]),
        invocation_id="active_inv",
    )
    subsequent_user_event = MockEvent(
        author="user",
        content=MockEventContent([MockEventPart("Later guidance in same turn")]),
        invocation_id="active_inv",
    )

    ctx = MockWorkflowContext(state={})
    ctx.invocation_id = "active_inv"
    ctx.session = MockSession(events=[prior_event, initial_user_event, subsequent_user_event])

    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "Synthesized answer."
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "Vaswani et al. 2017 Transformer architecture")

        assert received_synth_prompt is not None
        assert "# User Query\nHow do transformers work?" in received_synth_prompt
        assert "Old query from previous turn" not in received_synth_prompt
        assert "Later guidance in same turn" not in received_synth_prompt
        assert "# Target Search Goal\nVaswani et al. 2017 Transformer architecture" in received_synth_prompt


# ---------------------------------------------------------------------------
# 28. Multi-Turn TOC Caching, Coverage Gating & Context Continuity
# ---------------------------------------------------------------------------


def test_is_item_cached_primary_and_secondary():
    from zocrates.agents.library_search.search_pipeline import _is_item_cached

    cached = {
        "PARENT1": {"title": "Paper 1"},
        "ATT2": {"title": "Attachment 2"},
    }

    # Primary match on parent item_key
    assert _is_item_cached({"item_key": "PARENT1"}, cached) is True
    assert _is_item_cached({"key": "PARENT1"}, cached) is True

    # Secondary match on child attachment
    item_with_children = {
        "item_key": "PARENT2",
        "children": [
            {"item_key": "ATT2", "content_type": "application/pdf"}
        ]
    }
    assert _is_item_cached(item_with_children, cached) is True

    # Disjoint item not in cache
    item_disjoint = {
        "item_key": "PARENT3",
        "children": [
            {"item_key": "ATT3", "content_type": "application/pdf"}
        ]
    }
    assert _is_item_cached(item_disjoint, cached) is False
    assert _is_item_cached({}, cached) is False
    assert _is_item_cached({"item_key": "PARENT1"}, {}) is False


def test_get_applicable_cached_tocs_coverage_gate():
    from zocrates.agents.library_search.search_pipeline import _get_applicable_cached_tocs

    cached = {
        "ITEM_A": {"outline_text": "## Outline A\n- [s0] Abstract"},
        "ITEM_B": {"outline_text": "## Outline B\n- [s0] Abstract"},
    }

    # 1. 100% coverage with 1 item -> injected
    res1 = _get_applicable_cached_tocs([{"item_key": "ITEM_A"}], cached)
    assert len(res1) == 1
    assert "## Outline A" in res1[0]

    # 2. 100% coverage with 2 items -> both injected
    res2 = _get_applicable_cached_tocs([{"item_key": "ITEM_A"}, {"item_key": "ITEM_B"}], cached)
    assert len(res2) == 2
    assert "## Outline A" in res2[0]
    assert "## Outline B" in res2[1]

    # 3. Partial coverage (ITEM_C uncached) -> empty (Complete Active Coverage Gate: 0 asymmetric bias)
    res_partial = _get_applicable_cached_tocs([{"item_key": "ITEM_A"}, {"item_key": "ITEM_C"}], cached)
    assert res_partial == []

    # 4. Whole library search (empty active selection) -> empty
    assert _get_applicable_cached_tocs([], cached) == []
    assert _get_applicable_cached_tocs(None, cached) == []

    # 5. Over capacity (> 3 items) -> empty
    four_items = [{"item_key": f"ITEM_{i}"} for i in range(4)]
    four_cached = {f"ITEM_{i}": {"outline_text": f"## Outline {i}"} for i in range(4)}
    assert _get_applicable_cached_tocs(four_items, four_cached, max_items=3) == []


def test_has_context_continuity():
    from zocrates.agents.library_search.search_pipeline import _has_context_continuity

    # Both active and overlapping -> True
    assert _has_context_continuity([{"item_key": "A"}], [{"item_key": "A"}]) is True
    assert _has_context_continuity([{"item_key": "A"}, {"item_key": "B"}], [{"item_key": "B"}]) is True

    # Both active but disjoint -> False
    assert _has_context_continuity([{"item_key": "A"}], [{"item_key": "C"}]) is False

    # Both empty (whole library) -> True
    assert _has_context_continuity([], []) is True
    assert _has_context_continuity(None, None) is True
    assert _has_context_continuity([], None) is True

    # Scope changed between active and whole library -> False
    assert _has_context_continuity([{"item_key": "A"}], []) is False
    assert _has_context_continuity([], [{"item_key": "A"}]) is False
    assert _has_context_continuity([{"item_key": "A"}], None) is False


@pytest.mark.asyncio
async def test_search_pipeline_subagent_receives_cached_outline_with_soft_directive():
    from zocrates.agents.common.state_keys import CACHED_TOCS

    ctx = MockWorkflowContext(
        state={
            ACTIVE_CONTEXT_ITEMS: [{"item_key": "DOC1", "title": "Paper One"}],
            CACHED_TOCS: {
                "DOC1": {
                    "outline_text": "### [Key: DOC1, Parent Key: P1] \"Paper One\"\n- [s0] Abstract (~100 words)\n- [s1] Intro (~500 words)",
                }
            },
        }
    )

    received_search_input = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_search_input
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            received_search_input = kwargs.get("node_input", "")
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "title": "Paper One", "text": "Evidence", "word_count": 50, "chunk_type": "text"}
            ]
            return "search_done"
        if "synthesizer" in node_name:
            return "synth_done"
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"title": "Paper One", "text": "Evidence", "word_count": 50, "chunk_type": "text"}
        ]

        await search_pipeline_node._func(ctx, "Investigate Paper One")

        assert received_search_input is not None
        assert "## Pre-Retrieved Document Outline(s) for Active Items:" in received_search_input
        assert "### [Key: DOC1, Parent Key: P1] \"Paper One\"" in received_search_input
        assert "*(Note: Table of Contents is already provided above for these active items." in received_search_input
        assert "You can call `read_document` using your chosen asset IDs with either key.)*" in received_search_input
        assert "# Target Search Goal\nInvestigate Paper One" in received_search_input


@pytest.mark.asyncio
async def test_search_pipeline_prior_summary_injection_and_audit_schema():
    ctx = MockWorkflowContext(
        state={
            ACTIVE_CONTEXT_ITEMS: [{"item_key": "DOC1", "title": "Paper One"}],
            RETRIEVED_EVIDENCE: [
                {
                    "user_query": "Initial inquiry",
                    "search_query": "Initial disambiguated search",
                    "active_context_items": [{"item_key": "DOC1", "title": "Paper One"}],
                    "search_report": {
                        "source": "subagent_conclude",
                        "summary": "Established baseline accuracy of 92.5%.",
                    },
                    "evidence_chunks": [],
                }
            ],
        }
    )

    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c2", "title": "Paper One", "text": "New Evidence", "word_count": 50, "chunk_type": "text"}
            ]
            return "search_done"
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "synth_done"
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = [
            {"title": "Paper One", "text": "New Evidence", "word_count": 50, "chunk_type": "text"}
        ]

        await search_pipeline_node._func(ctx, "Follow-up inquiry on hyperparameters")

        # 1. Prior Turn Context injected into synth_prompt
        assert received_synth_prompt is not None
        assert "# Prior Turn Context (Read-Only Reference)" in received_synth_prompt
        assert 'Previous Inquiry: "Initial disambiguated search"' in received_synth_prompt
        assert "Previous Findings Summary: Established baseline accuracy of 92.5%." in received_synth_prompt

        # 2. Audit history entry schema validation
        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == 2
        latest_audit = audit_history[-1]
        assert "active_context_items" in latest_audit
        assert latest_audit["active_context_items"] == [{"item_key": "DOC1", "title": "Paper One"}]
        assert latest_audit["search_query"] == "Follow-up inquiry on hyperparameters"


@pytest.mark.asyncio
async def test_search_pipeline_prior_summary_omitted_for_auto_generated():
    ctx = MockWorkflowContext(
        state={
            ACTIVE_CONTEXT_ITEMS: [{"item_key": "DOC1", "title": "Paper One"}],
            RETRIEVED_EVIDENCE: [
                {
                    "user_query": "Initial inquiry",
                    "search_query": "Initial search",
                    "active_context_items": [{"item_key": "DOC1", "title": "Paper One"}],
                    "search_report": {
                        "source": "auto_generated",
                        "summary": "Mechanical auto-generated summary that should not be injected.",
                    },
                    "evidence_chunks": [],
                }
            ],
        }
    )

    received_synth_prompt = None

    async def mock_run_node(node_callable, *args, **kwargs):
        nonlocal received_synth_prompt
        node_name = getattr(node_callable, "name", str(node_callable))
        if "synthesizer" in node_name:
            received_synth_prompt = kwargs.get("node_input", "")
            return "synth_done"
        return "done"

    ctx.run_node.side_effect = mock_run_node

    with patch(
        "zocrates.agents.library_search.search_pipeline.rerank_evidence",
        new_callable=AsyncMock,
    ) as mock_rerank:
        mock_rerank.return_value = []

        await search_pipeline_node._func(ctx, "Next turn")

        assert received_synth_prompt is not None
        assert "# Prior Turn Context" not in received_synth_prompt
        assert "Mechanical auto-generated summary" not in received_synth_prompt


@pytest.mark.asyncio
async def test_search_pipeline_node_citation_verification_telemetry_audit():
    """Verifies that verification telemetry is captured and persisted in the audit record."""
    ctx = MockWorkflowContext(
        state={USER_PROMPT: "Explain somatic mutations", RETRIEVED_EVIDENCE: []}
    )

    async def mock_run_node(node_callable, *args, **kwargs):
        node_name = getattr(node_callable, "name", str(node_callable))
        if "search" in node_name:
            ctx.state[TEMP_RETRIEVED_EVIDENCE] = [
                {"chunk_id": "c1", "text": "Findings", "word_count": 100, "item_key": "K1"}
            ]
            return "done"
        return "Synthesized explanation with mutation rate findings [1]."

    ctx.run_node.side_effect = mock_run_node

    async def fake_verify_citations_support(text, ranked_chunks, config=None, telemetry_out=None):
        if telemetry_out is not None:
            telemetry_out.update({
                "schema_version": 1,
                "provider": "openrouter",
                "batch_size": 1,
                "summary": {"kept": 1, "demoted": 0, "fail_open": 0},
            })
        return {1: [0.95]}

    with (
        patch(
            "zocrates.agents.library_search.search_pipeline.load_zocrates_config",
            return_value={"citation_verification_enabled": True},
        ),
        patch(
            "zocrates.agents.library_search.search_pipeline.rerank_evidence",
            new_callable=AsyncMock,
        ) as mock_rerank,
        patch(
            "zocrates.agents.library_search.citation_verifier.verify_citations_support",
            side_effect=fake_verify_citations_support,
        ),
    ):
        mock_rerank.return_value = [
            {"chunk_id": "c1", "text": "Findings", "word_count": 100, "item_key": "K1"}
        ]

        await search_pipeline_node._func(ctx, "somatic mutations in twins")

        audit_history = ctx.state[RETRIEVED_EVIDENCE]
        assert len(audit_history) == 1
        entry = audit_history[0]
        assert entry.get("citation_verification_telemetry") is not None
        assert entry["citation_verification_telemetry"]["schema_version"] == 1
        assert entry["citation_verification_telemetry"]["batch_size"] == 1



