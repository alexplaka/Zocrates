"""End-to-end integration tests for Phase 2.3 Human-In-The-Loop (HITL) retrieval and dynamic workflow components.

These tests drive the real FastAPI application (``zocrates.fast_api_app``) through ``TestClient``,
verifying:
- Checkpoint interrupts yielding ``request_input`` Server-Sent Events over ``POST /chat``
- Resuming execution and streaming follow-up retrieval and synthesis over ``POST /chat/resume``
- Concurrency guards returning 409 Conflict when an interrupt is pending
- Extension ceilings and multi-grant progression up to the 2-extension cap
- State persistence and rehydration across SQLite database reloads
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from zocrates.agents.app import coordinator_agent, set_agent_models
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app as fast_app
from zocrates.rag import service_manager
from zocrates.rag.vector_store import SearchResult
from zocrates.utils import services
from zocrates.utils.services import reset_session_service
from tests.integration.test_coordinator_agent import ScriptedLlm, make_mock_syncer, stream_chat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stream_resume(
    client: TestClient,
    session_id: str,
    invocation_id: str,
    interrupt_id: str,
    response: dict,
) -> tuple[int, list[dict]]:
    """POST /chat/resume and return ``(status_code, parsed_sse_payloads)``."""
    payload = {
        "session_id": session_id,
        "invocation_id": invocation_id,
        "interrupt_id": interrupt_id,
        "response": response,
    }
    with client.stream("POST", "/chat/resume", json=payload) as resp:
        sse_payloads: list[dict] = []
        for line in resp.iter_lines():
            if line and line.startswith("data:"):
                raw_json = line[len("data:"):].strip()
                if raw_json:
                    try:
                        sse_payloads.append(json.loads(raw_json))
                    except Exception:
                        pass
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
    """TestClient running the real FastAPI app with offline InMemorySessionService."""
    assert isinstance(
        services.get_session_service(), InMemorySessionService
    ), "Integration tests require offline InMemorySessionService"
    with TestClient(fast_app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as c:
        yield c


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


def test_hitl_call_budget_exhaustion_triggers_request_input_sse(client: TestClient):
    """Subagent executes up to call budget limit (10 calls), calls conclude_search with gaps -> yields request_input SSE."""
    script = [
        # 1. Coordinator issues search_library
        types.FunctionCall(name="search_library", args={"request": "Survey multi-modal agent frameworks"}),
    ]
    # 2. Subagent executes 10 retrieval tool calls to exhaust budget
    for i in range(10):
        script.append(types.FunctionCall(name="search_corpus", args={"query": f"framework {i}", "limit": 10}))
    # 3. Subagent concludes search noting unresolved literature gaps
    script.append(
        types.FunctionCall(
            name="conclude_search",
            args={
                "summary": "Retrieved 10 candidate papers on multi-modal frameworks.",
                "gaps": ["Missing comparative ablation benchmarks between Gemini and Claude"],
            },
        )
    )
    script.append("Search concluded.")

    register_mock_dialogue(script=script)
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(client, prompt="Survey multi-modal agents", session_id="sess-exhaust-1")
    assert status == 200

    # Locate request_input event
    req_inputs = [p for p in payloads if p.get("event_type") == "request_input"]
    assert len(req_inputs) == 1, f"Expected 1 request_input event, got {len(req_inputs)}"

    ri = req_inputs[0]
    assert ri.get("interrupt_id")
    assert ri.get("invocation_id")

    payload = ri.get("payload", {})
    assert payload.get("type") == "deep_research_extension"
    assert payload.get("extension_number") == 1
    assert payload.get("max_extensions") == 2
    assert payload.get("current_calls") == 10
    assert "Retrieved 10 candidate papers" in payload.get("covered_summary", "")
    assert "Missing comparative ablation benchmarks" in payload.get("missing_aspects", [])[0]


def test_hitl_proactive_request_search_extension_triggers_request_input_sse(client: TestClient):
    """Subagent proactively calls request_search_extension before hitting limits -> yields request_input SSE."""
    script = [
        types.FunctionCall(name="search_library", args={"request": "Deep research on somatic mutations"}),
        types.FunctionCall(name="search_corpus", args={"query": "somatic mutation twins", "limit": 10}),
        types.FunctionCall(
            name="request_search_extension",
            args={
                "justification": "Initial search revealed 4 high-priority candidate studies needing section reads",
                "covered_summary": "Identified Jonsson et al. and related twin cohorts",
                "missing_aspects": ["Section 4 mutation rates", "Section 5 germline controls"],
            },
        ),
    ]

    register_mock_dialogue(script=script)
    service_manager._syncer = make_mock_syncer()

    status, payloads = stream_chat(client, prompt="Investigate somatic mutations", session_id="sess-proactive-1")
    assert status == 200

    req_inputs = [p for p in payloads if p.get("event_type") == "request_input"]
    assert len(req_inputs) == 1

    ri = req_inputs[0]
    payload = ri.get("payload", {})
    assert payload.get("extension_number") == 1
    assert "Identified Jonsson et al." in payload.get("covered_summary", "")
    assert "Section 4 mutation rates" in payload.get("missing_aspects", [])


def test_hitl_resume_with_extend_budget_and_synthesis(client: TestClient):
    """User grants extension -> POST /chat/resume executes additional retrieval tools and produces synthesized answer."""
    script = [
        types.FunctionCall(name="search_library", args={"request": "Compare attention vs convolution"}),
        types.FunctionCall(
            name="request_search_extension",
            args={
                "justification": "Need to fetch ablation table 2 from Vaswani et al.",
                "covered_summary": "Retrieved transformer architecture overview",
                "missing_aspects": ["Table 2 ablation benchmarks"],
            },
        ),
        # --- Resumed Turn Execution ---
        types.FunctionCall(name="search_corpus", args={"query": "ablation table 2", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Retrieved Table 2 ablation metrics successfully.", "gaps": []},
        ),
        "Synthesized grounded response: Attention exhibits superior parallel scaling across all benchmarks.",
    ]

    recorded_requests = []
    register_mock_dialogue(script=script, record=recorded_requests)
    service_manager._syncer = make_mock_syncer()

    # 1. Start chat and pause at HITL checkpoint
    status, payloads = stream_chat(client, prompt="Compare architectures", session_id="sess-resume-ext")
    assert status == 200
    ri = [p for p in payloads if p.get("event_type") == "request_input"][0]

    # 2. Resume with extend_budget and user guidance
    res_status, res_payloads = stream_resume(
        client,
        session_id="sess-resume-ext",
        invocation_id=ri["invocation_id"],
        interrupt_id=ri["interrupt_id"],
        response={"action": "extend_budget", "guidance": "Focus on Table 2 metrics"},
    )
    assert res_status == 200
    assert res_payloads

    # Assert search_corpus and conclude_search executed on resume
    res_fc_names = [
        part["function_call"]["name"]
        for p in res_payloads
        for part in p.get("parts", [])
        if "function_call" in part
    ]
    assert "search_corpus" in res_fc_names
    assert "conclude_search" in res_fc_names

    # Assert final synthesis streamed to client
    synth_text = " ".join(
        part.get("text", "")
        for p in res_payloads
        for part in p.get("parts", [])
        if part.get("text")
    )
    assert "Attention exhibits superior parallel scaling" in synth_text

    # Assert that no LLM prompt content was contaminated with ADK cross-agent fencing
    assert recorded_requests
    all_req_texts = [
        getattr(part, "text", "") or ""
        for req in recorded_requests
        for content in (getattr(req, "contents", []) or [])
        for part in (getattr(content, "parts", []) or [])
        if getattr(part, "text", None)
    ]
    assert not any("BEGIN_QUOTED_AGENT_CONTENT" in t for t in all_req_texts)
    assert not any("search_budget_checkpoint" in t for t in all_req_texts)


def test_hitl_resume_with_synthesize_immediately(client: TestClient):
    """User selects synthesize -> POST /chat/resume bypasses additional retrieval and synthesizes current evidence."""
    script = [
        types.FunctionCall(name="search_library", args={"request": "Overview of quantum neural nets"}),
        types.FunctionCall(name="search_corpus", args={"query": "quantum neural nets", "limit": 10}),
        types.FunctionCall(
            name="request_search_extension",
            args={
                "justification": "More experimental papers could be examined",
                "covered_summary": "Retrieved theoretical formulation",
                "missing_aspects": ["Hardware benchmarks on superconducting qubits"],
            },
        ),
        # --- Resumed Turn: Synthesizer produces answer immediately ---
        "Synthesized grounded response with existing evidence: Quantum neural nets offer theoretical advantages despite hardware gaps.",
    ]

    register_mock_dialogue(script=script)
    service_manager._syncer = make_mock_syncer()

    # 1. Start chat and pause
    status, payloads = stream_chat(client, prompt="Overview of QNN", session_id="sess-resume-synth")
    assert status == 200
    ri = [p for p in payloads if p.get("event_type") == "request_input"][0]

    # 2. Resume with synthesize
    res_status, res_payloads = stream_resume(
        client,
        session_id="sess-resume-synth",
        invocation_id=ri["invocation_id"],
        interrupt_id=ri["interrupt_id"],
        response={"action": "synthesize"},
    )
    assert res_status == 200

    # Verify no additional retrieval calls were issued during resume
    res_fc_names = [
        part["function_call"]["name"]
        for p in res_payloads
        for part in p.get("parts", [])
        if "function_call" in part
    ]
    assert "search_corpus" not in res_fc_names
    assert "get_document_toc" not in res_fc_names

    # Verify synthesizer generated the answer
    synth_text = " ".join(
        part.get("text", "")
        for p in res_payloads
        for part in p.get("parts", [])
        if part.get("text")
    )
    assert "Quantum neural nets offer theoretical advantages" in synth_text


def test_hitl_pending_interrupt_returns_409_on_new_chat(client: TestClient):
    """Calling POST /chat while an interrupt is pending returns HTTP 409 Conflict."""
    script = [
        types.FunctionCall(name="search_library", args={"request": "Study somatic mutations"}),
        types.FunctionCall(
            name="request_search_extension",
            args={
                "justification": "Need extension",
                "covered_summary": "Covered preliminary cohort",
                "missing_aspects": ["Control cohort validation"],
            },
        ),
    ]

    register_mock_dialogue(script=script)
    service_manager._syncer = make_mock_syncer()

    # 1. Start chat and pause
    status, payloads = stream_chat(client, prompt="Study somatic mutations", session_id="sess-409-test")
    assert status == 200
    assert any(p.get("event_type") == "request_input" for p in payloads)

    # 2. Attempt a fresh POST /chat on the same session while interrupt is unresolved
    resp = client.post(
        "/chat",
        json={
            "prompt": "Brand new prompt while interrupted",
            "session_id": "sess-409-test",
            "mode": "dialogue",
        },
    )
    assert resp.status_code == 409
    assert "Session has a pending research extension request" in resp.json()["detail"]


def test_hitl_second_extension_and_hard_stop_on_third(client: TestClient):
    """Extension 1 and Extension 2 can be granted; after Extension 2, no 3rd extension is triggered and synthesis proceeds."""
    record = []
    script = [
        # Pass 1
        types.FunctionCall(name="search_library", args={"request": "Comprehensive LLM reasoning survey"}),
        types.FunctionCall(
            name="request_search_extension",
            args={"justification": "Need ext 1", "covered_summary": "Covered 5 models", "missing_aspects": ["Reasoning benchmarks"]},
        ),
        # Pass 2 (After Extension 1 granted)
        types.FunctionCall(name="search_corpus", args={"query": "reasoning benchmarks", "limit": 10}),
        types.FunctionCall(
            name="request_search_extension",
            args={"justification": "Need ext 2", "covered_summary": "Covered reasoning benchmarks", "missing_aspects": ["Safety evaluations"]},
        ),
        # Pass 3 (After Extension 2 granted: max 2 extensions reached, hard stop)
        types.FunctionCall(name="search_corpus", args={"query": "safety evaluations", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Covered safety evaluations.", "gaps": ["Unaddressed out-of-domain alignment"]},
        ),
        "Final comprehensive synthesis covering models, reasoning benchmarks, and safety evaluations.",
    ]

    register_mock_dialogue(script=script, record=record)
    service_manager._syncer = make_mock_syncer()

    # 1. Pass 1 -> Pause on Extension 1
    status1, payloads1 = stream_chat(client, prompt="Comprehensive survey", session_id="sess-multi-ext")
    assert status1 == 200
    ri1 = [p for p in payloads1 if p.get("event_type") == "request_input"][0]
    assert ri1["payload"]["extension_number"] == 1

    # 2. Resume Pass 1 -> Pause on Extension 2
    status2, payloads2 = stream_resume(
        client,
        session_id="sess-multi-ext",
        invocation_id=ri1["invocation_id"],
        interrupt_id=ri1["interrupt_id"],
        response={"action": "extend_budget"},
    )
    assert status2 == 200
    ri2 = [p for p in payloads2 if p.get("event_type") == "request_input"][0]
    assert ri2["payload"]["extension_number"] == 2

    # 3. Resume Pass 2 -> Hard limit reached -> Must proceed directly to synthesis (no 3rd request_input)
    status3, payloads3 = stream_resume(
        client,
        session_id="sess-multi-ext",
        invocation_id=ri2["invocation_id"],
        interrupt_id=ri2["interrupt_id"],
        response={"action": "extend_budget"},
    )
    assert status3 == 200

    # Ensure no 3rd request_input was triggered
    req_inputs3 = [p for p in payloads3 if p.get("event_type") == "request_input"]
    assert len(req_inputs3) == 0

    # Ensure final synthesis was produced
    synth_text = " ".join(
        part.get("text", "")
        for p in payloads3
        for part in p.get("parts", [])
        if part.get("text")
    )
    assert "Final comprehensive synthesis" in synth_text

    # Verify that search_extension_1 was executed during Pass 1 (2 LLM calls for the 2 scripted actions)
    # and was NOT re-executed during Resume Pass 2 (which would have produced 4 calls)
    ext1_prompts = [
        r for r in record
        if any("Extension 1 of 2" in (getattr(p, "text", None) or "") for c in getattr(r, "contents", []) for p in getattr(c, "parts", []))
    ]
    assert len(ext1_prompts) == 2, "Extension 1 should execute its 2 tool calls and not re-execute on Extension 2 resume"

    # Verify that search_extension_2 received 'Extension 2 of 2'
    ext2_prompts = [
        r for r in record
        if any("Extension 2 of 2" in (getattr(p, "text", None) or "") for c in getattr(r, "contents", []) for p in getattr(c, "parts", []))
    ]
    assert len(ext2_prompts) == 2, "Extension 2 should execute with 'Extension 2 of 2' prompt"

    # Verify clean isolation scope closure via finish_task
    import asyncio
    from google.adk.runners import _find_active_task_scope
    session = asyncio.run(
        services.get_session_service().get_session(
            app_name="zocrates", user_id="default_user", session_id="sess-multi-ext"
        )
    )
    assert _find_active_task_scope(session) is None, "Scope should be cleanly closed after synthesis"


def test_hitl_isolation_scope_preservation_and_event_filtering(client: TestClient):
    """Subagent on resume retains isolation scope, drops prior coordinator turns, and avoids fencing."""
    record = []
    script = [
        # Turn 1: Conversational banter
        "Hello! I am Zocrates coordinator. How can I help you?",
        # Turn 2: Research query
        types.FunctionCall(name="search_library", args={"request": "Survey multi-agent debate"}),
        types.FunctionCall(
            name="request_search_extension",
            args={"justification": "Need debate benchmarks", "covered_summary": "Found initial papers", "missing_aspects": ["Debate benchmarks"]},
        ),
        # Turn 2 Resumed: Extension 1
        types.FunctionCall(name="search_corpus", args={"query": "debate benchmarks", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Retrieved debate benchmarks.", "gaps": []},
        ),
        "Final synthesis on multi-agent debate and benchmarks.",
    ]

    register_mock_dialogue(script=script, record=record)
    service_manager._syncer = make_mock_syncer()

    # 1. Turn 1: Conversational banter (coordinator runs unscoped)
    status0, payloads0 = stream_chat(client, prompt="hi", session_id="sess-iso-test")
    assert status0 == 200

    # 2. Turn 2: Research query -> pauses at interrupt
    status1, payloads1 = stream_chat(client, prompt="Survey multi-agent debate", session_id="sess-iso-test")
    assert status1 == 200
    ri = [p for p in payloads1 if p.get("event_type") == "request_input"][0]

    # 3. Resume with extension
    res_status, res_payloads = stream_resume(
        client,
        session_id="sess-iso-test",
        invocation_id=ri["invocation_id"],
        interrupt_id=ri["interrupt_id"],
        response={"action": "extend_budget"},
    )
    assert res_status == 200

    # 4. Inspect recorded requests for the search subagent during Extension 1 (2 LLM calls: search_corpus, conclude_search)
    subagent_requests = [
        r for r in record
        if any("Extension 1 of 2" in (getattr(p, "text", None) or "") for c in getattr(r, "contents", []) for p in getattr(c, "parts", []))
    ]
    assert len(subagent_requests) == 2, "Extension 1 subagent executes its 2 tool turns"
    subagent_req = subagent_requests[0]

    # Assert that coordinator banter is NOT present in subagent's content
    all_texts = " ".join(
        getattr(p, "text", None) or ""
        for c in getattr(subagent_req, "contents", [])
        for p in getattr(c, "parts", [])
        if getattr(p, "text", None)
    )
    assert "Hello! I am Zocrates coordinator" not in all_texts
    assert "[coordinator_agent] said:" not in all_texts

    # Assert that subagent's events on resume were stamped with search_pipeline isolation scope
    import asyncio
    from google.adk.runners import _find_active_task_scope
    session = asyncio.run(
        services.get_session_service().get_session(
            app_name="zocrates", user_id="default_user", session_id="sess-iso-test"
        )
    )
    search_events = [
        ev for ev in session.events
        if getattr(ev, "author", None) in ("library_search_agent", "search_subagent")
    ]
    assert len(search_events) > 0
    for ev in search_events:
        assert getattr(ev, "isolation_scope", "").startswith(("search_library_pipeline:", "search_pipeline:"))

    # Assert active task scope is cleanly closed after synthesis
    assert _find_active_task_scope(session) is None


def test_hitl_sqlite_persistence_and_resume_roundtrip(tmp_path: Path):
    """Driving interrupt and resume against real SQLite DatabaseSessionService preserves hitl_snapshot across reloads."""
    db_file = tmp_path / "test_hitl_sessions.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    sqlite_service = DatabaseSessionService(db_url=db_url)

    # Configure services to use SQLite session service
    services._session_service = sqlite_service

    script = [
        types.FunctionCall(name="search_library", args={"request": "Investigate CRISPR Cas9 methods"}),
        types.FunctionCall(
            name="request_search_extension",
            args={
                "justification": "Need off-target cleavage studies",
                "covered_summary": "Retrieved primary sgRNA protocols",
                "missing_aspects": ["Off-target cleavage assays"],
            },
        ),
        # Resumed turn
        types.FunctionCall(name="search_corpus", args={"query": "off-target cleavage", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Retrieved off-target cleavage assay results.", "gaps": []},
        ),
        "Synthesized CRISPR Cas9 report covering sgRNA and off-target cleavage assays.",
    ]

    register_mock_dialogue(script=script)
    service_manager._syncer = make_mock_syncer()

    with TestClient(fast_app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
        # 1. Turn 1: runs against SQLite and pauses at checkpoint
        status, payloads = stream_chat(client, prompt="CRISPR protocols", session_id="sess-sqlite-hitl")
        assert status == 200
        ri = [p for p in payloads if p.get("event_type") == "request_input"][0]

        # 2. Simulate complete restart / reload: create fresh DatabaseSessionService pointing to same db_file
        fresh_sqlite_service = DatabaseSessionService(db_url=db_url)
        services._session_service = fresh_sqlite_service
        fast_app.state.runner.session_service = fresh_sqlite_service

        # 3. Resume against the reloaded SQLite service
        res_status, res_payloads = stream_resume(
            client,
            session_id="sess-sqlite-hitl",
            invocation_id=ri["invocation_id"],
            interrupt_id=ri["interrupt_id"],
            response={"action": "extend_budget", "guidance": "Examine high-fidelity Cas9 variants"},
        )
        assert res_status == 200

        # Assert search executed and synthesis succeeded
        synth_text = " ".join(
            part.get("text", "")
            for p in res_payloads
            for part in p.get("parts", [])
            if part.get("text")
        )
        assert "Synthesized CRISPR Cas9 report" in synth_text


def test_hitl_second_research_turn_in_same_session_does_not_skip_search_initial(client: TestClient):
    """A second research turn in the same session executes search_initial normally without skipping."""
    record = []
    script = [
        # Turn 1: Research query with extension and synthesis
        types.FunctionCall(name="search_library", args={"request": "First research topic"}),
        types.FunctionCall(
            name="request_search_extension",
            args={"justification": "Need more data", "covered_summary": "Initial findings", "missing_aspects": ["Aspect A"]},
        ),
        # Turn 1 Resume: Extension 1
        types.FunctionCall(name="search_corpus", args={"query": "aspect A", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Retrieved aspect A.", "gaps": []},
        ),
        "Synthesis for first topic.",
        # Turn 2: Second research query in the SAME session
        types.FunctionCall(name="search_library", args={"request": "Second research topic"}),
        types.FunctionCall(name="search_corpus", args={"query": "second topic query", "limit": 10}),
        types.FunctionCall(
            name="conclude_search",
            args={"summary": "Retrieved second topic findings.", "gaps": []},
        ),
        "Synthesis for second topic.",
    ]

    register_mock_dialogue(script=script, record=record)
    service_manager._syncer = make_mock_syncer()

    # 1. Turn 1 starts -> pauses at checkpoint
    status1, payloads1 = stream_chat(client, prompt="Tell me about first topic", session_id="sess-two-turns")
    assert status1 == 200
    ri = [p for p in payloads1 if p.get("event_type") == "request_input"][0]

    # 2. Turn 1 resumed -> completes synthesis
    res_status, res_payloads = stream_resume(
        client,
        session_id="sess-two-turns",
        invocation_id=ri["invocation_id"],
        interrupt_id=ri["interrupt_id"],
        response={"action": "extend_budget"},
    )
    assert res_status == 200
    synth_text1 = " ".join(
        part.get("text", "")
        for p in res_payloads
        for part in p.get("parts", [])
        if part.get("text")
    )
    assert "Synthesis for first topic" in synth_text1

    # 3. Turn 2: Ask about the second research topic in the same session
    status2, payloads2 = stream_chat(client, prompt="Now tell me about second topic", session_id="sess-two-turns")
    assert status2 == 200

    synth_text2 = " ".join(
        part.get("text", "")
        for p in payloads2
        for part in p.get("parts", [])
        if part.get("text")
    )
    assert "Synthesis for second topic" in synth_text2

    # Verify that Turn 2 executed search_corpus for second topic (i.e. did NOT skip search_initial)
    t2_search_corpus_calls = [
        r for r in record
        if any("second topic query" in str(getattr(p, "function_response", {})) or "second topic query" in str(getattr(p, "function_call", {}))
               for c in getattr(r, "contents", []) for p in getattr(c, "parts", []))
    ]
    assert len(t2_search_corpus_calls) > 0, "Turn 2 should NOT skip search_initial; it must execute search_corpus"

    # Verify scope is cleanly closed after Turn 2
    import asyncio
    from google.adk.runners import _find_active_task_scope
    session = asyncio.run(
        services.get_session_service().get_session(
            app_name="zocrates", user_id="default_user", session_id="sess-two-turns"
        )
    )
    assert _find_active_task_scope(session) is None, "Scope should be cleanly closed after Turn 2"

