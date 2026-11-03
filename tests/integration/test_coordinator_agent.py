"""End-to-end integration tests for the Zocrates streaming chat endpoint (``POST /chat``).

These tests drive the *real* FastAPI application (``zocrates.fast_api_app``) through
``TestClient``: the ADK lifespan starts a real ``Runner`` bound to the root
coordinator agent (``coordinator_agent``), and each ``POST /chat``
streams real Server-Sent Events.

LLMs are replaced with a deterministic scripted ``BaseLlm`` mock so the suite
runs in seconds with no external network calls, no 429s, and no cloud API keys.
Agent models are configured per test, and all
module-level state (``service_manager._syncer``, session service)
is reset around each test.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from zocrates.agents.app import coordinator_agent, set_agent_models
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app as fast_app
from zocrates.rag import service_manager
from zocrates.rag.vector_store import SearchResult
from zocrates.utils import services
from zocrates.utils.services import reset_session_service


# ---------------------------------------------------------------------------
# Deterministic scripted mock LLM
# ---------------------------------------------------------------------------


class ScriptedLlm(BaseLlm):
    """Deterministic mock LLM that plays a script and records every request.

    Each ``generate_content_async`` call pops the next scripted item, which may
    be a plain ``str`` (text reply), a ``types.FunctionCall``, or a full
    ``types.Content``. Every ``LlmRequest`` is appended to ``recorded`` so
    tests can assert on exact prompt contents (e.g. multi-turn history and
    active-context injection).
    """

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
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "stream":
            for chunk in item[1]:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part.from_text(text=chunk)]),
                    partial=True,
                )
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part.from_text(text="".join(item[1]))]),
                partial=False,
            )
            return
        if isinstance(item, tuple) and len(item) == 3 and item[0] == "stream_fc":
            for pfc in item[1]:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(function_call=pfc)]),
                    partial=True,
                )
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(function_call=item[2])]),
                partial=False,
            )
            return
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


def text_from_request(llm_request) -> str:
    """Join all text parts across an LlmRequest's contents (for assertions)."""
    out: list[str] = []
    for content in llm_request.contents:
        if not content or not content.parts:
            continue
        for part in content.parts:
            if getattr(part, "text", None):
                out.append(part.text)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_syncer(tmp_path: Path | None = None) -> MagicMock:
    """Return a mock LibrarySyncer exposing the store/db surface used by the chat path."""
    syncer = MagicMock()
    syncer.close = MagicMock()
    syncer._zotero_data_dir = tmp_path or Path("/tmp/mock_zotero")
    syncer.db.get_item_keys_in_collection.return_value = []
    syncer.db.get_note_by_key.return_value = None
    syncer.db.get_item.return_value = SimpleNamespace(title="Attention Paper")
    syncer.db.get_item_by_key.return_value = SimpleNamespace(title="Attention Paper")
    syncer.store.search.return_value = [
        SearchResult(
            chunk_text=(
                "The Transformer architecture relies on self-attention to "
                "compute representations of all positions simultaneously."
            ),
            item_key="ITEM123",
            item_id=1,
            heading_path=["1 Introduction"],
            chunk_index=0,
            score=0.9,
            chunk_type="text",
            chunk_id="ITEM123::0",
            line_start=1,
            line_end=4,
        )
    ]
    return syncer


def stream_chat(
    client: TestClient,
    prompt: str,
    session_id: str,
    mode: str = "dialogue",
    active_items: list[dict] | None = None,
) -> tuple[int, list[dict]]:
    """POST /chat and return ``(status_code, parsed_sse_payloads)``."""
    payload = {
        "prompt": prompt,
        "session_id": session_id,
        "mode": mode,
    }
    if active_items is not None:
        payload["active_items"] = active_items

    with client.stream("POST", "/chat", json=payload) as resp:
        sse_payloads: list[dict] = []
        for line in resp.iter_lines():
            if line and line.startswith("data:"):
                sse_payloads.append(json.loads(line[len("data:"):]))
        return resp.status_code, sse_payloads


def register_mock_dialogue(script: list, record: list | None = None) -> ScriptedLlm:
    """Register a mock-backed coordinator."""
    mock = ScriptedLlm(script=script, record=record)
    set_agent_models(mock)
    return mock


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
def reset_zocrates_state(tmp_path, monkeypatch):
    """Reset module-level services and agent models around each test."""
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    monkeypatch.setenv("ZOTERO_DATA_DIR", str(tmp_path))
    service_manager._syncer = None
    reset_session_service()
    yield
    service_manager._syncer = None
    reset_session_service()
    set_agent_models()


@pytest.fixture
def client():
    """TestClient running the real FastAPI app (starts the ADK Runner lifespan)."""
    assert isinstance(
        services.get_session_service(), InMemorySessionService
    ), "Integration tests require offline InMemorySessionService"
    with TestClient(fast_app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Registration guard
# ---------------------------------------------------------------------------


def test_chat_endpoint_requires_registration(client: TestClient):
    """POST /chat raises HTTP 400 when the backend has not been registered."""
    # service_manager._syncer is None (reset by the autouse fixture), so
    # get_syncer() inside the endpoint raises RuntimeError -> HTTP 400.
    status, _ = stream_chat(
        client, prompt="Hello", session_id="sess-unregistered", mode="dialogue"
    )
    assert status == 400


# ---------------------------------------------------------------------------
# 2. Single-turn streaming
# ---------------------------------------------------------------------------


def test_chat_endpoint_single_turn_streaming(client: TestClient, caplog):
    """A plain chat turn streams a final text part over SSE."""
    register_mock_dialogue(script=["Hello there! How can I help you?"])
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(
        client, prompt="Hello", session_id="sess-1", mode="dialogue"
    )
    assert status == 200
    assert payloads, "Expected at least one SSE payload"

    # Verify wire contract: every SSE payload must contain id, author, is_final, partial, parts
    for p in payloads:
        assert "id" in p, f"Payload missing 'id': {p}"
        assert "author" in p, f"Payload missing 'author': {p}"
        assert "is_final" in p, f"Payload missing 'is_final': {p}"
        assert "partial" in p, f"Payload missing 'partial': {p}"
        assert "parts" in p, f"Payload missing 'parts': {p}"

    final_texts = [
        part["text"]
        for p in payloads
        if p.get("author") == "coordinator_agent"
        for part in p.get("parts", [])
        if "text" in part
    ]
    assert final_texts, "Expected a text part from the coordinator agent"
    assert any("Hello there!" in t for t in final_texts)

    coordinator_events = [p for p in payloads if p.get("author") == "coordinator_agent"]
    assert coordinator_events, "Expected coordinator events in the SSE stream"
    assert any(p.get("is_final") is True for p in coordinator_events)


def test_chat_endpoint_token_streaming_sse_deltas(client: TestClient):
    """Progressive SSE streaming emits partial=True token deltas followed by a partial=False aggregated event."""
    chunks = ["Transformers ", "use ", "multi-head ", "attention."]
    register_mock_dialogue(script=[("stream", chunks)])
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(
        client, prompt="Tell me about transformers", session_id="sess-stream-deltas", mode="dialogue"
    )
    assert status == 200
    assert payloads, "Expected SSE payloads"

    # Separate partial deltas from final complete event for coordinator
    coord_payloads = [p for p in payloads if p.get("author") == "coordinator_agent"]
    partial_payloads = [p for p in coord_payloads if p.get("partial") is True]
    final_payloads = [p for p in coord_payloads if p.get("partial") is False and p.get("parts")]

    assert len(partial_payloads) == len(chunks), f"Expected {len(chunks)} partial events, got {len(partial_payloads)}"
    partial_texts = [
        part["text"]
        for p in partial_payloads
        for part in p.get("parts", [])
        if "text" in part
    ]
    assert partial_texts == chunks

    assert final_payloads, "Expected at least one non-partial event carrying aggregated text"
    final_text = "".join(
        part["text"]
        for p in final_payloads
        for part in p.get("parts", [])
        if "text" in part
    )
    assert final_text == "".join(chunks)


def test_chat_endpoint_partial_function_call_suppression(client: TestClient):
    """Partial function call chunks are suppressed from SSE stream payloads; only final FC is emitted."""
    partial_fcs = [
        types.FunctionCall(name="library_search", args={"request": '{"req'}),
        types.FunctionCall(name="library_search", args={"request": '{"request": "trans'}),
    ]
    final_fc = types.FunctionCall(name="library_search", args={"request": "transformers"})
    register_mock_dialogue(
        script=[
            ("stream_fc", partial_fcs, final_fc),
            # Pipeline completes with direct mock text
            "Final pipeline synthesis",
        ]
    )
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(
        client, prompt="Find transformer papers", session_id="sess-stream-fc", mode="dialogue"
    )
    assert status == 200
    assert payloads

    # Collect all payloads that contain a function_call part
    fc_payloads = [
        p for p in payloads
        for part in p.get("parts", [])
        if "function_call" in part and part["function_call"]["name"] == "library_search"
    ]
    # Verify that partial function calls were NOT emitted
    for p in fc_payloads:
        assert p.get("partial") is False, f"Unexpected partial function call payload: {p}"

    # Exactly one complete function_call event should be emitted for library_search
    assert len(fc_payloads) == 1, f"Expected 1 complete library_search call, got {len(fc_payloads)}"
    fc_args = fc_payloads[0]["parts"][0]["function_call"]["args"]
    assert fc_args == {"request": "transformers"}


# ---------------------------------------------------------------------------
# 3. Multi-turn history persistence
# ---------------------------------------------------------------------------


def test_chat_endpoint_multi_turn_history_persistence(client: TestClient):
    """Turn 2 sees Turn 1 prompt + reply + Turn 2 prompt, and messages persist."""
    record: list = []
    register_mock_dialogue(
        script=[
            "Self-attention relates all positions in a single pass.",
            "RNNs process sequentially, while attention processes in parallel.",
        ],
        record=record,
    )
    service_manager._syncer = make_mock_syncer()

    # Turn 1
    status_1, _ = stream_chat(
        client,
        prompt="What is self-attention?",
        session_id="sess-mt",
        mode="dialogue",
    )
    assert status_1 == 200

    # Turn 2 on the same session
    status_2, payloads_2 = stream_chat(
        client,
        prompt="How does it compare to RNNs?",
        session_id="sess-mt",
        mode="dialogue",
    )
    assert status_2 == 200
    turn2_texts = [
        part["text"]
        for p in payloads_2
        for part in p.get("parts", [])
        if "text" in part
    ]
    assert any("RNNs process sequentially" in t for t in turn2_texts)

    # Turn 2 LLM request must contain the full conversation history
    assert len(record) >= 2, "Expected at least one LLM request per turn"
    turn2_contents = text_from_request(record[1])
    assert "What is self-attention?" in turn2_contents, "Turn 1 prompt missing"
    assert "Self-attention relates all positions" in turn2_contents, "Turn 1 reply missing"
    assert "How does it compare to RNNs?" in turn2_contents, "Turn 2 prompt missing"

    # Session persistence via GET /sessions/{id}
    res = client.get("/sessions/sess-mt")
    assert res.status_code == 200
    session = res.json()["session"]
    messages = session["messages"]
    assert messages, "Expected persisted messages for the session"
    user_msgs = [m["text"] for m in messages if m["role"] == "user"]
    assistant_msgs = [m["text"] for m in messages if m["role"] == "assistant"]
    assert any("What is self-attention?" in t for t in user_msgs)
    assert any("How does it compare to RNNs?" in t for t in user_msgs)
    assert any("Self-attention relates all positions" in t for t in assistant_msgs)


# ---------------------------------------------------------------------------
# 4. Active Zotero selection context injection
# ---------------------------------------------------------------------------


def test_chat_endpoint_active_items_context_injection(client: TestClient):
    """Active Zotero selection chips are injected into the LLM turn input."""
    record: list = []
    register_mock_dialogue(script=["Understood, analyzing the selected paper."], record=record)
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(
        client,
        prompt="Summarize the methods",
        session_id="sess-active",
        mode="dialogue",
        active_items=[{"key": "ITEM123", "title": "Attention Paper", "type": "item"}],
    )
    assert status == 200
    assert payloads

    assert len(record) >= 1
    first_request_text = text_from_request(record[0])
    assert "# Context: Active Items Selected in Zotero" in first_request_text
    assert "Attention Paper" in first_request_text
    # Internal Zotero item keys are hidden from the Coordinator prompt to prevent leakage
    assert "ITEM123" not in first_request_text


# ---------------------------------------------------------------------------
# 5. Mode switching & fallback
# ---------------------------------------------------------------------------


def test_chat_endpoint_mode_switching_and_fallback(client: TestClient):
    """Chat endpoint serves coordinator regardless of legacy mode parameter."""
    register_mock_dialogue(
        script=[
            "Dialogue turn reply",
            "Fallback reply (discovery)",
            "Fallback reply (synthesis)",
            "Fallback reply (unknown)",
        ]
    )
    service_manager._syncer = make_mock_syncer()

    with patch(
        "zocrates.agents.common.compaction.run_adaptive_post_invocation_compaction",
        new_callable=AsyncMock,
        return_value=False,
    ):
        # Turn 1: normal dialogue
        status_1, payloads_1 = stream_chat(
            client, prompt="Hello", session_id="sess-mode", mode="dialogue"
        )
        assert status_1 == 200
        assert any(
            "Dialogue turn reply" in part["text"]
            for p in payloads_1
            for part in p.get("parts", [])
            if "text" in part
        )

        # Turn 2: discovery -> answered by coordinator
        status_2, payloads_2 = stream_chat(
            client, prompt="Explore", session_id="sess-mode", mode="discovery"
        )
        assert status_2 == 200
        assert any(
            "Fallback reply (discovery)" in part["text"]
            for p in payloads_2
            for part in p.get("parts", [])
            if "text" in part
        )

        # Turn 3: synthesis -> answered by coordinator
        status_3, payloads_3 = stream_chat(
            client, prompt="Synthesize", session_id="sess-mode", mode="synthesis"
        )
        assert status_3 == 200
        assert any(
            "Fallback reply (synthesis)" in part["text"]
            for p in payloads_3
            for part in p.get("parts", [])
            if "text" in part
        )

        # Turn 4: unknown mode -> answered by coordinator
        status_4, payloads_4 = stream_chat(
            client, prompt="Unknown", session_id="sess-mode", mode="bogus_mode"
        )
        assert status_4 == 200
        assert any(
            "Fallback reply (unknown)" in part["text"]
            for p in payloads_4
            for part in p.get("parts", [])
            if "text" in part
        )


# ---------------------------------------------------------------------------
# 6. Tool call flow (search pipeline)
# ---------------------------------------------------------------------------


def test_chat_endpoint_tool_call_flow(client: TestClient):
    """Coordinator delegates to search_library; the search pipeline executes and streams results."""
    register_mock_dialogue(
        script=[
            # 1. Coordinator delegates literature research to the search pipeline.
            types.FunctionCall(
                name="search_library",
                args={"request": "Tell me about attention mechanisms in transformers"},
            ),
            # 2-3. Search subagent runs search_corpus and reports a chunk.
            types.FunctionCall(name="search_corpus", args={"query": "attention", "limit": 10}),
            "Found section 1 of Attention Is All You Need describing self-attention.",
            # 4. Synthesizer produces the grounded answer that becomes the tool result.
            "Synthesized grounded answer: self-attention relates all positions in parallel.",
        ]
    )
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(
        client, prompt="Explain self-attention", session_id="sess-tool", mode="dialogue"
    )
    assert status == 200
    assert payloads

    # 1. The coordinator issued the search_library tool call.
    search_library_call = [
        p
        for p in payloads
        for part in p.get("parts", [])
        if "function_call" in part
        and part["function_call"]["name"] == "search_library"
    ]
    assert search_library_call, "Expected a search_library function_call event"

    # 2. The search pipeline executed (search subagent issued search_corpus).
    search_corpus_call = [
        p
        for p in payloads
        for part in p.get("parts", [])
        if "function_call" in part
        and part["function_call"]["name"] == "search_corpus"
    ]
    assert search_corpus_call, "Expected the search pipeline to run search_corpus"

    # 3. The search_corpus function response reached the stream.
    search_responses = [
        p
        for p in payloads
        for part in p.get("parts", [])
        if "function_response" in part
        and part["function_response"]["name"] == "search_corpus"
    ]
    assert search_responses, "Expected a search_corpus function_response event"

    # 4. The synthesizer produced the grounded answer surfaced to the user.
    synthesized_sse_text = " ".join(
        part.get("text", "")
        for p in payloads
        if p.get("author") in ("library_synthesizer_agent", "synthesizer_agent")
        for part in p.get("parts", [])
        if "text" in part
    )
    assert "Synthesized grounded answer" in synthesized_sse_text

    # 5. The final coordinator event relays the grounded answer verbatim
    #    (skip_summarization=True on the library_search tool).
    relayed_text = " ".join(
        json.dumps(part, default=str)
        for p in payloads
        if p.get("author") == "coordinator_agent" and p.get("is_final") is True
        for part in p.get("parts", [])
    )
    assert "Synthesized grounded answer" in relayed_text


# ---------------------------------------------------------------------------
# 7. Session management lifecycle
# ---------------------------------------------------------------------------


def test_session_management_lifecycle(client: TestClient):
    """POST /sessions -> /chat -> GET /sessions/{id} -> DELETE -> GET 404."""
    register_mock_dialogue(script=["Hello! Ask me about your literature."])
    service_manager._syncer = make_mock_syncer()

    # Create session
    res = client.post("/sessions", json={"title": "Integration Test Chat"})
    assert res.status_code == 200
    session_id = res.json()["session"]["id"]
    assert session_id

    # Chat turn
    status, payloads = stream_chat(
        client, prompt="Hi", session_id=session_id, mode="dialogue"
    )
    assert status == 200
    assert payloads

    # GET persisted messages
    res = client.get(f"/sessions/{session_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    messages = data["session"]["messages"]
    assert any(m["role"] == "user" and m["text"].strip() == "Hi" for m in messages)
    assert any(
        m["role"] == "assistant" and "Hello! Ask me" in m["text"] for m in messages
    )

    # Delete session
    res = client.delete(f"/sessions/{session_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # GET after delete -> 404
    res = client.get(f"/sessions/{session_id}")
    assert res.status_code == 404