"""Integration tests for the Online Literature Discovery pipeline and SSE chat endpoint."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from zocrates.agents.app import coordinator_agent, set_agent_models
from zocrates.agents.online_search.agent import (
    create_online_search_agent,
    create_online_synthesizer_agent,
)
from zocrates.agents.online_search.search_pipeline import search_online_pipeline_node
from zocrates.config import get_auth_token
from zocrates.discovery.models import CandidatePaper
from zocrates.fast_api_app import app as fast_app
from zocrates.rag import service_manager
from zocrates.utils import services
from zocrates.utils.services import reset_session_service


# ---------------------------------------------------------------------------
# Scripted Mock LLM
# ---------------------------------------------------------------------------


class ScriptedLlm(BaseLlm):
    """Deterministic mock LLM that returns scripted responses."""

    model: str = "mock-llm"
    script: list = []
    recorded: list = []

    def __init__(self, script=None, record=None, **kwargs):
        super().__init__(model=kwargs.pop("model", "mock-llm"), **kwargs)
        self.script = list(script or [])
        self.recorded = record if record is not None else []

    async def generate_content_async(self, req, stream: bool = False):
        self.recorded.append(req)
        item = self.script.pop(0) if self.script else "Mock fallback text."
        if isinstance(item, str):
            content = types.Content(
                role="model", parts=[types.Part.from_text(text=item)]
            )
        elif isinstance(item, types.FunctionCall):
            content = types.Content(
                role="model", parts=[types.Part(function_call=item)]
            )
        else:
            content = item
        yield LlmResponse(content=content)


def _make_candidate(
    cid: str,
    title: str,
    year: int = 2024,
    authors: list[str] | None = None,
    is_oa: bool = True,
    citation_count: int = 50,
) -> CandidatePaper:
    return CandidatePaper(
        candidate_id=cid,
        title=title,
        authors=authors or ["Dr. Author", "Dr. Coauthor"],
        year=year,
        venue="ICLR",
        doi=f"10.1234/{cid}",
        arxiv_id=f"2402.{cid}",
        abstract=f"Groundbreaking findings on {title}.",
        is_oa=is_oa,
        citation_count=citation_count,
        primary_topic="Language Models",
        keywords=["compression", "kv cache", "llm inference"],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    """Reset the sse_starlette AppStatus exit event across TestClient event loops."""
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


@pytest.fixture(autouse=True)
def reset_zocrates_state(monkeypatch):
    """Reset module-level services and agent models around each test."""
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    service_manager._syncer = None
    reset_session_service()
    yield
    service_manager._syncer = None
    reset_session_service()
    set_agent_models()


@pytest.fixture
def client():
    """TestClient running the real FastAPI app with InMemorySessionService."""
    assert isinstance(
        services.get_session_service(), InMemorySessionService
    ), "Integration tests require offline InMemorySessionService"
    with TestClient(fast_app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as c:
        yield c


def stream_chat(client: TestClient, prompt: str, session_id: str = "sess-online-int") -> tuple[int, list[dict]]:
    """Helper to issue POST /chat and collect parsed SSE payloads."""
    resp = client.post(
        "/chat",
        json={"prompt": prompt, "session_id": session_id},
    )
    sse_payloads = []
    for line in resp.iter_lines():
        if line and line.startswith("data:"):
            sse_payloads.append(json.loads(line[len("data:"):].strip()))
    return resp.status_code, sse_payloads


def stream_resume(
    client: TestClient,
    session_id: str,
    invocation_id: str,
    interrupt_id: str,
    response: dict,
) -> tuple[int, list[dict]]:
    """Helper to issue POST /chat/resume and collect parsed SSE payloads."""
    payload = {
        "session_id": session_id,
        "invocation_id": invocation_id,
        "interrupt_id": interrupt_id,
        "response": response,
    }
    with client.stream("POST", "/chat/resume", json=payload) as resp:
        parsed = []
        for line in resp.iter_lines():
            if line and line.startswith("data:"):
                parsed.append(json.loads(line[len("data:"):].strip()))
        return resp.status_code, parsed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_online_pipeline_node_simulated_run():
    """Simulated end-to-end execution of search_online_pipeline_node with mocked OpenAlex."""
    cand1 = _make_candidate("W1001", "Fast KV Cache Eviction", 2024)
    cand2 = _make_candidate("W1002", "Quantized KV Cache Tables", 2023)

    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=[cand1, cand2])

    search_script = [
        # Subagent issues OpenAlex query
        types.FunctionCall(
            name="search_openalex",
            args={"query": "kv cache compression transformers", "year_from": 2023},
        ),
        # After tool returns candidates, subagent concludes
        types.FunctionCall(
            name="conclude_online_search",
            args={
                "recommendations": [
                    {
                        "candidate_id": "W1001",
                        "rationale": "Pioneers dynamic eviction based on attention score decay for long-context inference.",
                    },
                    {
                        "candidate_id": "W1002",
                        "rationale": "Quantizes cached key-value states to 4 bits with negligible perplexity loss.",
                    },
                ],
                "summary": "Selected top KV cache compression works",
            },
        ),
    ]

    synth_script = [
        (
            "Recent advancements in KV cache compression focus on dynamic token eviction [1] and multi-bit quantization [2]."
        )
    ]

    search_llm = ScriptedLlm(script=search_script)
    synth_llm = ScriptedLlm(script=synth_script)

    search_subagent = create_online_search_agent(model=search_llm)
    synth_subagent = create_online_synthesizer_agent(model=synth_llm)

    class MockParentAgent:
        _subagents = {
            "online_search": search_subagent,
            "online_synthesizer": synth_subagent,
        }

    class MockInvContext:
        agent = MockParentAgent()
        invocation_id = "inv-online-sim"
        branch = None

        def __init__(self):
            self.enqueued_events = []

        async def _enqueue_event(self, ev):
            self.enqueued_events.append(ev)

    inv_ctx = MockInvContext()

    class MockContext:
        state = {
            "discovery_engine": mock_engine,
        }
        _invocation_context = inv_ctx
        isolation_scope = "search_online_pipeline:fc-sim-run"
        agent_name = "search_online_pipeline_node"

        async def run_node(self, node_callable, *args, **kwargs):
            if hasattr(node_callable, "name") and node_callable.name == "online_search_agent":
                # Execute single turn subagent step 1
                res1 = None
                async for r in search_subagent.model.generate_content_async(None):
                    res1 = r
                part1 = res1.content.parts[0]
                if part1.function_call:
                    from zocrates.agents.online_search.tools import search_openalex
                    tool_ctx = SimpleNamespace(
                        state=self.state,
                        actions=SimpleNamespace(skip_summarization=False),
                    )
                    await search_openalex(**part1.function_call.args, tool_context=tool_ctx)

                # Execute single turn subagent step 2
                res2 = None
                async for r in search_subagent.model.generate_content_async(None):
                    res2 = r
                part2 = res2.content.parts[0]
                if part2.function_call:
                    from zocrates.agents.online_search.tools import conclude_online_search
                    tool_ctx = SimpleNamespace(
                        state=self.state,
                        actions=SimpleNamespace(skip_summarization=False),
                    )
                    conclude_online_search(**part2.function_call.args, tool_context=tool_ctx)
                return "Search finished"

            elif hasattr(node_callable, "name") and node_callable.name == "online_synthesizer_agent":
                synth_res = None
                async for r in synth_subagent.model.generate_content_async(None):
                    synth_res = r
                return synth_res.content.parts[0].text
            return ""

    ctx = MockContext()
    result = await search_online_pipeline_node._func(ctx, "kv cache compression techniques")

    assert "Recommended Papers for Import:" not in result
    assert "Recent advancements in KV cache compression" in result

    # Verify candidates saved in state
    assert "candidates" in ctx.state
    candidates = ctx.state["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["candidate_id"] == "W1001"
    assert (
        candidates[0]["tldr"]
        == "Pioneers dynamic eviction based on attention score decay for long-context inference."
    )
    assert candidates[1]["candidate_id"] == "W1002"
    assert (
        candidates[1]["tldr"]
        == "Quantizes cached key-value states to 4 bits with negligible perplexity loss."
    )

    # Verify isolation scope closure
    assert len(inv_ctx.enqueued_events) == 1
    closing = inv_ctx.enqueued_events[0]
    assert closing.author == "search_online_pipeline_node"
    assert closing.isolation_scope == "search_online_pipeline:fc-sim-run"
    assert closing.content.parts[0].function_response.name == "finish_task"
    assert closing.content.parts[0].function_response.response == {"result": "Task completed."}


def test_chat_endpoint_search_online_literature_flow(client: TestClient):
    """Coordinator delegates to search_online_literature and candidate metadata is delivered in SSE stream."""
    cand1 = _make_candidate("W2001", "Speculative Streaming Architecture", 2024)
    cand2 = _make_candidate("W2002", "Tree-based Draft Verification", 2024)

    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=[cand1, cand2])

    synth_text = (
        "Here are novel candidate preprints on speculative streaming:\n\n"
        "### Recommended Papers for Import:\n"
        "- [1]: Low-latency speculative streaming architecture for edge devices.\n"
        "- [2]: Tree-structured candidate verification algorithm.\n"
    )

    script = [
        # 1. Coordinator receives user prompt and calls search_online_literature
        types.FunctionCall(
            name="search_online_literature",
            args={"request": "Find speculative streaming preprints on arXiv"},
        ),
        # 2. Online search subagent calls search_openalex
        types.FunctionCall(
            name="search_openalex",
            args={"query": "speculative streaming preprints"},
        ),
        # 3. Online search subagent concludes search
        types.FunctionCall(
            name="conclude_online_search",
            args={
                "recommendations": [
                    {
                        "candidate_id": "W2001",
                        "rationale": "Low-latency speculative streaming architecture for edge devices.",
                    },
                    {
                        "candidate_id": "W2002",
                        "rationale": "Tree-structured candidate verification algorithm.",
                    },
                ],
                "summary": "Discovered speculative streaming works",
            },
        ),
        # 4. Synthesizer outputs recommendations
        synth_text,
    ]

    mock_llm = ScriptedLlm(script=script)
    set_agent_models(mock_llm)

    syncer = MagicMock()
    syncer.close = MagicMock()
    syncer._zotero_data_dir = Path("/tmp/mock_zotero")
    syncer.db.get_item_keys_in_collection.return_value = []
    syncer.db.get_item.return_value = SimpleNamespace(title="Attention Paper")
    syncer.db.get_item_by_key.return_value = SimpleNamespace(title="Attention Paper")
    service_manager._syncer = syncer

    with patch(
        "zocrates.agents.online_search.tools.get_default_discovery_engine",
        return_value=mock_engine,
    ):
        status, payloads = stream_chat(
            client,
            prompt="Find speculative streaming preprints online",
            session_id="sess-online-flow",
        )

    assert status == 200
    assert payloads

    # Verify coordinator issued search_online_literature call
    online_calls = [
        p
        for p in payloads
        for part in p.get("parts", [])
        if "function_call" in part
        and part["function_call"]["name"] == "search_online_literature"
    ]
    assert online_calls, "Expected search_online_literature function call in SSE stream"

    # Verify candidate metadata is delivered in the final SSE payload
    final_payloads = [p for p in payloads if p.get("is_final")]
    assert final_payloads, "Expected final SSE payload"
    final_p = final_payloads[-1]

    assert "metadata" in final_p
    assert "candidates" in final_p["metadata"]
    delivered_candidates = final_p["metadata"]["candidates"]
    assert len(delivered_candidates) == 2
    assert delivered_candidates[0]["candidate_id"] == "W2001"
    assert delivered_candidates[1]["candidate_id"] == "W2002"
    assert delivered_candidates[0]["title"] == "Speculative Streaming Architecture"


def test_chat_resume_online_search_pipeline(client: TestClient):
    """Interrupted online search session successfully resumes via POST /chat/resume."""
    cand = _make_candidate("W3001", "Resumed Discovery Paper", 2024)
    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=[cand])

    script = [
        # 1. Coordinator delegates to search_online_literature
        types.FunctionCall(
            name="search_online_literature",
            args={"request": "Discover new preprints"},
        ),
        # 2. Subagent requests an extension proactively
        types.FunctionCall(
            name="request_search_extension",
            args={"rationale": "Need to explore adjacent preprints on arXiv"},
        ),
        # --- Resumed Turn Execution ---
        # 3. Subagent calls search_openalex
        types.FunctionCall(
            name="search_openalex",
            args={"query": "resumed discovery preprints"},
        ),
        # 4. Subagent concludes
        types.FunctionCall(
            name="conclude_online_search",
            args={
                "recommendations": [
                    {
                        "candidate_id": "W3001",
                        "rationale": "Novel resumed discovery architecture.",
                    },
                ],
                "summary": "Found target preprint",
            },
        ),
        # 5. Synthesizer outputs recommendations
        (
            "Here is the synthesized overview:\n\n"
            "### Recommended Papers for Import:\n"
            "- [1]: Novel resumed discovery architecture.\n"
        ),
    ]

    mock_llm = ScriptedLlm(script=script)
    set_agent_models(mock_llm)

    syncer = MagicMock()
    syncer.close = MagicMock()
    syncer._zotero_data_dir = Path("/tmp/mock_zotero")
    syncer.db.get_item_keys_in_collection.return_value = []
    syncer.db.get_item.return_value = SimpleNamespace(title="Attention Paper")
    syncer.db.get_item_by_key.return_value = SimpleNamespace(title="Attention Paper")
    service_manager._syncer = syncer

    with patch(
        "zocrates.agents.online_search.tools.get_default_discovery_engine",
        return_value=mock_engine,
    ):
        # 1. Start chat and pause at HITL checkpoint
        status, payloads = stream_chat(
            client,
            prompt="Find new literature online",
            session_id="sess-online-resume",
        )
        assert status == 200
        ri_events = [p for p in payloads if p.get("event_type") == "request_input"]
        assert ri_events, "Expected request_input event from online search pipeline"
        ri = ri_events[0]

        # 2. Resume with extend_budget
        res_status, res_payloads = stream_resume(
            client,
            session_id="sess-online-resume",
            invocation_id=ri["invocation_id"],
            interrupt_id=ri["interrupt_id"],
            response={"action": "extend_budget", "guidance": "Focus on 2024 works"},
        )

        assert res_status == 200
        assert res_payloads

        # Verify search_openalex was invoked after resume
        openalex_calls = [
            part["function_call"]["name"]
            for p in res_payloads
            for part in p.get("parts", [])
            if "function_call" in part
            and part["function_call"]["name"] == "search_openalex"
        ]
        assert openalex_calls, "Expected search_openalex call after resume"

        # Verify final payload contains candidates
        final_res = [p for p in res_payloads if p.get("is_final")]
        assert final_res
        assert "metadata" in final_res[-1]
        assert "candidates" in final_res[-1]["metadata"]
        assert len(final_res[-1]["metadata"]["candidates"]) == 1
        assert final_res[-1]["metadata"]["candidates"][0]["candidate_id"] == "W3001"

