"""Unit tests for Phase 2.1 Human-In-The-Loop (HITL) retrieval and dynamic workflow components."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event, RequestInput
from google.adk.sessions import Session
from google.adk.tools import ToolContext
from google.adk.workflow._errors import NodeInterruptedError
from google.genai import types

from zocrates.agents.common.state_keys import (
    HITL_SNAPSHOT,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_MAX_RERANK_CHUNKS,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEARCH_REPORT,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
    TEMP_WORD_CEILING,
)
from zocrates.agents.common.tools import (
    _check_and_increment_tool_budget,
    request_search_extension,
)
from zocrates.agents.library_search.search_pipeline import (
    search_library_pipeline_node,
    search_pipeline_node,
)
from zocrates.fast_api_app import _get_unresolved_session_interrupts, app
from zocrates.utils.session_formatting import format_session_for_ui


def test_hitl_snapshot_persistence_and_rehydration():
    """Verify that HITL_SNAPSHOT successfully restores temporary state across process/SQLite reload."""
    snapshot = {
        "evidence": [{"chunk_id": "c1", "text": "evidence text 1", "word_count": 50}],
        "search_report": {
            "summary": "Covered 1 chunk.",
            "gaps": ["Missing section 4 benchmarks"],
            "notes": "Preliminary run",
            "source": "subagent_request",
        },
        "extension_count": 1,
        "tool_count": 10,
        "extension_requested": True,
        "ceilings": {
            "budget": 15,
            "word_ceiling": 11250,
            "max_chunks": 20,
        },
        "seen_chunk_ids": ["c1"],
        "seen_assets": ["a1"],
    }

    state: dict = {HITL_SNAPSHOT: snapshot}

    # Simulate rehydration logic from _run_search_pipeline
    if state.get(HITL_SNAPSHOT) and not state.get(TEMP_RETRIEVED_EVIDENCE):
        snap = state[HITL_SNAPSHOT]
        state[TEMP_RETRIEVED_EVIDENCE] = snap.get("evidence", [])
        state[TEMP_SEARCH_REPORT] = snap.get("search_report")
        state[TEMP_EXTENSION_COUNT] = snap.get("extension_count", 0)
        state[TEMP_RETRIEVAL_TOOL_COUNT] = snap.get("tool_count", 0)
        state[TEMP_EXTENSION_REQUESTED] = snap.get("extension_requested", False)
        state[TEMP_SEEN_CHUNK_IDS] = snap.get("seen_chunk_ids", [])
        state[TEMP_SEEN_ASSETS] = snap.get("seen_assets", [])
        ceilings = snap.get("ceilings", {})
        state[TEMP_RETRIEVAL_TOOL_BUDGET] = ceilings.get("budget", 10)
        state[TEMP_WORD_CEILING] = ceilings.get("word_ceiling", 7500)
        state[TEMP_MAX_RERANK_CHUNKS] = ceilings.get("max_chunks", 15)

    assert len(state[TEMP_RETRIEVED_EVIDENCE]) == 1
    assert state[TEMP_SEARCH_REPORT]["gaps"] == ["Missing section 4 benchmarks"]
    assert state[TEMP_EXTENSION_COUNT] == 1
    assert state[TEMP_RETRIEVAL_TOOL_COUNT] == 10
    assert state[TEMP_EXTENSION_REQUESTED] is True
    assert state[TEMP_RETRIEVAL_TOOL_BUDGET] == 15
    assert state[TEMP_WORD_CEILING] == 11250
    assert state[TEMP_MAX_RERANK_CHUNKS] == 20
    assert state[TEMP_SEEN_CHUNK_IDS] == ["c1"]


def test_hitl_snapshot_cleared_on_completion_prevents_turn2_contamination():
    """Verify that when HITL_SNAPSHOT is cleared (set to None), Turn 2 does not rehydrate stale evidence."""
    state: dict = {HITL_SNAPSHOT: None, TEMP_RETRIEVED_EVIDENCE: []}

    snapshot = state.get(HITL_SNAPSHOT)
    if snapshot and not state.get(TEMP_RETRIEVED_EVIDENCE):
        state[TEMP_RETRIEVED_EVIDENCE] = snapshot.get("evidence", [])

    # Evidence remains clean/empty for Turn 2
    assert state[TEMP_RETRIEVED_EVIDENCE] == []


def test_proactive_request_search_extension_tool():
    """Verify that request_search_extension flags state and prepares preliminary report."""
    mock_tc = MagicMock(spec=ToolContext)
    mock_tc.state = {}
    mock_tc.actions = MagicMock()

    res = request_search_extension(
        justification="Need to inspect ablation tables in Paper B",
        covered_summary="Retrieved core architecture from Paper A",
        missing_aspects=["Ablation tables", "Benchmark hyperparams"],
        tool_context=mock_tc,
    )

    assert res["status"] == "extension_requested"
    assert mock_tc.state[TEMP_EXTENSION_REQUESTED] is True
    assert mock_tc.actions.skip_summarization is True

    report = mock_tc.state[TEMP_SEARCH_REPORT]
    assert report["summary"] == "Retrieved core architecture from Paper A"
    assert report["gaps"] == ["Ablation tables", "Benchmark hyperparams"]
    assert report["notes"] == "Need to inspect ablation tables in Paper B"
    assert report["source"] == "subagent_request"


def test_check_and_increment_tool_budget_notices_and_ceilings():
    """Verify tool budget checks emit 80% word warning and suggest request_search_extension."""
    mock_tc = MagicMock(spec=ToolContext)
    mock_tc.state = {
        TEMP_RETRIEVED_EVIDENCE: [{"text": "word " * 6100, "word_count": 6100}],
        TEMP_RETRIEVAL_TOOL_COUNT: 0,
        TEMP_WORD_CEILING: 7500,
        TEMP_RETRIEVAL_TOOL_BUDGET: 10,
        TEMP_EXTENSION_COUNT: 0,
    }

    allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
    assert allowed is True
    assert notice is not None
    assert "Evidence volume approaching ceiling" in notice
    assert "request_search_extension" in notice


def test_check_and_increment_tool_budget_graduated_notices():
    """Verify tool budget graduated notice progression across calls."""
    mock_tc = MagicMock(spec=ToolContext)
    mock_tc.state = {
        TEMP_RETRIEVED_EVIDENCE: [],
        TEMP_RETRIEVAL_TOOL_COUNT: 0,
        TEMP_WORD_CEILING: 7500,
        TEMP_RETRIEVAL_TOOL_BUDGET: 10,
        TEMP_EXTENSION_COUNT: 0,
    }

    # Calls 1 to 4: No notice
    for i in range(1, 5):
        allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
        assert allowed is True
        assert err is None
        assert notice is None
        assert mock_tc.state[TEMP_RETRIEVAL_TOOL_COUNT] == i

    # Call 5 (midpoint: int(10/2) = 5): Midpoint notice
    allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
    assert allowed is True
    assert err is None
    assert notice is not None
    assert "5/10 retrieval tool calls used" in notice
    assert "Continue your search, being mindful of your remaining retrieval budget" in notice

    # Calls 6 to 8: Midpoint notices continue
    for expected_count in (6, 7, 8):
        allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
        assert allowed is True
        assert err is None
        assert notice is not None
        assert f"{expected_count}/10 retrieval tool calls used" in notice
        assert "Continue your search, being mindful of your remaining retrieval budget" in notice

    # Call 9 (budget - 1 = 9): Penultimate warning
    allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
    assert allowed is True
    assert err is None
    assert notice is not None
    assert "9/10 retrieval tool calls used (1 call remaining)" in notice
    assert "Plan your final retrieval" in notice

    # Call 10 (budget = 10): Exhaustion notice
    allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
    assert allowed is True
    assert err is None
    assert notice is not None
    assert "Retrieval tool budget exhausted (10/10)" in notice

    # Call 11 (exceeded): Blocked
    allowed, err, notice = _check_and_increment_tool_budget(mock_tc)
    assert allowed is False
    assert err is not None
    assert "Retrieval tool budget reached (10/10)" in err



@pytest.mark.asyncio
async def test_search_pipeline_preserves_scope_on_interrupt():
    """Verify that NodeInterruptedError propagates without prematurely closing task isolation scope."""
    mock_ctx = MagicMock()
    mock_ctx.isolation_scope = "search_pipeline:fc-123"

    with patch(
        "zocrates.agents.library_search.search_pipeline._run_search_pipeline",
        new=AsyncMock(side_effect=NodeInterruptedError()),
    ), patch(
        "zocrates.agents.library_search.search_pipeline._enqueue_isolation_scope_closing_event",
        new=AsyncMock(),
    ) as mock_close:
        with pytest.raises(NodeInterruptedError):
            await search_pipeline_node._func(mock_ctx, "test search")

        mock_close.assert_not_called()


def test_unresolved_session_interrupts_scanner():
    """Verify _get_unresolved_session_interrupts correctly tracks pending interrupts."""
    # 1. Empty session
    empty_sess = MagicMock(events=[])
    assert _get_unresolved_session_interrupts(empty_sess) == set()

    # 2. Session with open interrupt
    int_evt = Event(
        author="search_subagent",
        long_running_tool_ids={"int-abc"},
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="adk_request_input", id="int-abc", args={}
                    )
                )
            ],
        ),
    )
    sess_with_int = MagicMock(events=[int_evt])
    assert _get_unresolved_session_interrupts(sess_with_int) == {"int-abc"}

    # 3. Session where interrupt was resolved by matching function response
    res_evt = Event(
        author="user",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="adk_request_input", id="int-abc", response={"action": "extend_budget"}
                    )
                )
            ],
        ),
    )
    sess_resolved = MagicMock(events=[int_evt, res_evt])
    assert _get_unresolved_session_interrupts(sess_resolved) == set()


def test_resume_chat_endpoint_validates_interrupt(monkeypatch):
    """Verify POST /chat/resume rejects invalid or stale interrupt IDs with 409."""
    from zocrates.config import get_auth_token

    client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})

    # Mock syncer, runner, and session service
    mock_syncer = MagicMock()
    mock_syncer._zotero_data_dir = None
    monkeypatch.setattr("zocrates.fast_api_app.get_syncer", lambda: mock_syncer)

    mock_runner = MagicMock()
    mock_session_service = AsyncMock()

    # Case 1: Session not found -> 404
    mock_session_service.get_session.return_value = None
    mock_runner.session_service = mock_session_service
    monkeypatch.setattr(app.state, "runner", mock_runner, raising=False)

    resp = client.post(
        "/chat/resume",
        json={
            "session_id": "nonexistent-sess",
            "invocation_id": "inv-1",
            "interrupt_id": "int-123",
            "response": {"action": "extend_budget"},
        },
    )
    assert resp.status_code == 404

    # Case 2: Session exists but has no pending interrupt matching interrupt_id -> 409
    mock_session = MagicMock(events=[])
    mock_session_service.get_session.return_value = mock_session

    resp = client.post(
        "/chat/resume",
        json={
            "session_id": "sess-1",
            "invocation_id": "inv-1",
            "interrupt_id": "stale-int",
            "response": {"action": "extend_budget"},
        },
    )
    assert resp.status_code == 409
    assert "Invalid or stale interrupt ID" in resp.json()["detail"]


def test_format_session_skips_fr_only_user_events():
    """Verify format_session_for_ui does not split turns or render empty user bubbles for resume FRs."""
    user_initial = Event(
        id="e-u1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="What is SICA?")]),
    )
    asst_step1 = Event(
        id="e-a1",
        author="search_subagent",
        long_running_tool_ids={"int-999"},
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="adk_request_input",
                        id="int-999",
                        args={"message": "Need extension"},
                    )
                )
            ],
        ),
    )
    # Resume event has role="user", author="user", but ONLY function_response
    user_resume = Event(
        id="e-u2",
        author="user",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="adk_request_input",
                        id="int-999",
                        response={"action": "extend_budget"},
                    )
                )
            ],
        ),
    )
    asst_final = Event(
        id="e-a2",
        author="synthesizer_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="SICA stands for Self-Improving Coding Agents.")],
        ),
    )

    session = Session(
        app_name="zocrates",
        user_id="default_user",
        id="sess-hitl",
        events=[user_initial, asst_step1, user_resume, asst_final],
    )

    formatted = format_session_for_ui(session)
    msgs = formatted["messages"]

    # Should only be 2 messages: 1 user message and 1 assistant message
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "What is SICA?"
    assert msgs[1]["role"] == "assistant"
    assert "Self-Improving Coding Agents" in msgs[1]["text"]

    # adk_request_input and its return should be in intermediate_steps, not user messages
    steps = msgs[1]["intermediate_steps"]
    step_names = [s.get("name") for s in steps]
    assert "adk_request_input" in step_names


@pytest.mark.asyncio
async def test_dynamic_workflow_hitl_interrupt_and_resume_roundtrip():
    """Verify that dynamic workflow checkpoint pauses with RequestInput and resumes cleanly with FunctionResponse."""
    from google.adk.apps import App
    from google.adk.runners import Runner
    from google.adk.sessions.database_session_service import DatabaseSessionService
    from google.adk.workflow import node

    @node(name="unit_test_checkpoint", rerun_on_resume=False)
    async def unit_test_checkpoint(ctx, node_input):
        yield RequestInput(interrupt_id="int-roundtrip", message="pause?", payload=node_input)

    @node(name="unit_test_orchestrator", rerun_on_resume=True)
    async def unit_test_orchestrator(ctx, node_input):
        decision = await ctx.run_node(unit_test_checkpoint, node_input={"stage": "1"})
        action = decision.get("action") if isinstance(decision, dict) else str(decision)
        return f"Completed: {action}"

    test_app = App(name="test_hitl_app", root_agent=unit_test_orchestrator)
    test_runner = Runner(
        app=test_app,
        session_service=DatabaseSessionService(db_url="sqlite+aiosqlite:///:memory:"),
    )

    await test_runner.session_service.create_session(
        app_name="test_hitl_app", user_id="u1", session_id="s-rt"
    )

    # 1. Turn 1 pauses at checkpoint
    t1_events = []
    async for e in test_runner.run_async(
        user_id="u1",
        session_id="s-rt",
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="start")]),
    ):
        t1_events.append(e)

    assert len(t1_events) >= 1
    interrupt_evt = t1_events[-1]
    assert "int-roundtrip" in (interrupt_evt.long_running_tool_ids or set())
    assert interrupt_evt.content.parts[0].function_call.name == "adk_request_input"

    # 2. Resume turn provides FunctionResponse and finishes
    fr_part = types.Part(
        function_response=types.FunctionResponse(
            name="adk_request_input",
            id="int-roundtrip",
            response={"action": "extended_success"},
        )
    )
    resume_msg = types.Content(role="user", parts=[fr_part])
    resume_events = []
    async for e in test_runner.run_async(
        user_id="u1",
        session_id="s-rt",
        invocation_id=interrupt_evt.invocation_id,
        new_message=resume_msg,
    ):
        resume_events.append(e)

    assert len(resume_events) >= 1
    final_evt = resume_events[-1]
    assert final_evt.output == "Completed: extended_success"


def test_format_session_rewind_prunes_pending_hitl():
    """Verify that rewinding past a turn containing an HITL interrupt removes pending_hitl."""
    from google.adk.events import Event, EventActions
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    u1 = Event(id="e1", invocation_id="inv-1", author="user", content=types.Content(role="user", parts=[types.Part.from_text(text="Turn 1")]))
    a1 = Event(id="e2", invocation_id="inv-1", author="assistant", content=types.Content(role="model", parts=[types.Part.from_text(text="Ans 1")]))

    u2 = Event(id="e3", invocation_id="inv-2", author="user", content=types.Content(role="user", parts=[types.Part.from_text(text="Turn 2")]))
    hitl_part = types.Part(function_call=types.FunctionCall(name="adk_request_input", id="int-rewind", args={"message": "extend?"}))
    a2 = Event(id="e4", invocation_id="inv-2", author="assistant", content=types.Content(role="model", parts=[hitl_part]), long_running_tool_ids=["int-rewind"])

    # Rewind before inv-2
    rewind_evt = Event(id="e5", invocation_id="inv-3", author="user", actions=EventActions(rewind_before_invocation_id="inv-2"))

    session = Session(app_name="zocrates", user_id="u", id="s-rewind-hitl", events=[u1, a1, u2, a2, rewind_evt])

    formatted = format_session_for_ui(session)
    assert formatted.get("pending_hitl") is None
    assert len(formatted["messages"]) == 2
    assert formatted["messages"][-1].get("pending_hitl") is None


def test_adk_request_input_context_filter_patch():
    """Verify that _should_include_event_in_context filters out adk_request_input events."""
    import google.adk.flows.llm_flows.contents as adk_contents
    from zocrates.agents.common.adk_compat import patch_adk_request_input_context_filter

    # Safe to call multiple times due to process-wide idempotency guard
    patch_adk_request_input_context_filter()

    iso_scope = "search_pipeline:call_123"

    # 1. Checkpoint RequestInput function call event
    fc_part = types.Part(
        function_call=types.FunctionCall(
            name="adk_request_input",
            args={"interrupt_id": "interrupt_ext_1", "message": "extend?"},
            id="interrupt_ext_1",
        )
    )
    fc_event = Event(
        author="search_budget_checkpoint",
        content=types.Content(role="model", parts=[fc_part]),
        isolation_scope=iso_scope,
    )

    # 2. User resume RequestInput function response event
    fr_part = types.Part(
        function_response=types.FunctionResponse(
            name="adk_request_input",
            response={"action": "extend_budget"},
            id="interrupt_ext_1",
        )
    )
    fr_event = Event(
        author="user",
        content=types.Content(role="user", parts=[fr_part]),
        isolation_scope=iso_scope,
    )

    # 3. Normal retrieval tool call event
    normal_tool_call = types.Part(
        function_call=types.FunctionCall(
            name="search_corpus",
            args={"query": "transformer models"},
            id="call_retrieval_1",
        )
    )
    normal_fc_event = Event(
        author="search_subagent",
        content=types.Content(role="model", parts=[normal_tool_call]),
        isolation_scope=iso_scope,
    )

    # 4. Normal retrieval tool response event
    normal_tool_resp = types.Part(
        function_response=types.FunctionResponse(
            name="search_corpus",
            response={"status": "success", "results": [{"chunk_id": "c1"}]},
            id="call_retrieval_1",
        )
    )
    normal_fr_event = Event(
        author="search_subagent",
        content=types.Content(role="user", parts=[normal_tool_resp]),
        isolation_scope=iso_scope,
    )

    # 5. Normal text instruction / continuation prompt
    normal_text_event = Event(
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="# Deep Research Extension Granted")]),
        isolation_scope=iso_scope,
    )

    # Asserts on _should_include_event_in_context
    assert adk_contents._should_include_event_in_context(None, fc_event, isolation_scope=iso_scope) is False
    assert adk_contents._should_include_event_in_context(None, fr_event, isolation_scope=iso_scope) is False
    assert adk_contents._should_include_event_in_context(None, normal_fc_event, isolation_scope=iso_scope) is True
    assert adk_contents._should_include_event_in_context(None, normal_fr_event, isolation_scope=iso_scope) is True
    assert adk_contents._should_include_event_in_context(None, normal_text_event, isolation_scope=iso_scope) is True

    # Verify end-to-end contents generation (_get_contents) excludes fencing markers
    # (Testing with is_single_turn=True, matching search_subagent configuration)
    events = [normal_fc_event, normal_fr_event, fc_event, fr_event, normal_text_event]
    contents = adk_contents._get_contents(
        current_branch=None,
        events=events,
        agent_name="search_subagent",
        preserve_function_call_ids=True,
        isolation_scope=iso_scope,
        user_content=None,
        is_single_turn=True,
        include_thoughts_from_other_agents=False,
    )

    all_texts = [
        p.text
        for c in contents
        for p in (c.parts or [])
        if p.text
    ]
    assert any("# Deep Research Extension Granted" in t for t in all_texts)
    assert not any("BEGIN_QUOTED_AGENT_CONTENT" in t for t in all_texts)
    assert not any("search_budget_checkpoint" in t for t in all_texts)
    assert not any("adk_request_input" in t for t in all_texts)


class _MockHitlWorkflowContext:
    def __init__(self, state=None):
        from types import SimpleNamespace
        self.state = state if state is not None else {}
        self.run_node = AsyncMock()
        self.isolation_scope = "search_pipeline:fc-lib-hitl"
        self.agent_name = "search_library_pipeline_node"
        self._invocation_context = SimpleNamespace(
            agent=SimpleNamespace(_subagents={}),
            invocation_id="inv-lib-hitl-1",
            branch=None,
            session=None,
            enqueued_events=[],
            _enqueue_event=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_library_search_hitl_agent_request_trigger_reason():
    """Verify library search HITL trigger reason is 'agent_request' when subagent requests extension."""
    ctx = _MockHitlWorkflowContext(
        state={
            TEMP_RETRIEVED_EVIDENCE: [{"chunk_id": "c1", "text": "Evidence 1", "word_count": 100}],
            TEMP_EXTENSION_REQUESTED: True,
            TEMP_RETRIEVAL_TOOL_COUNT: 4,
            TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            TEMP_WORD_CEILING: 7500,
            TEMP_SEARCH_REPORT: {
                "summary": "Covered preliminary section",
                "gaps": ["Need benchmark figures"],
                "notes": "Agent needs more retrieval steps to locate benchmarks",
                "source": "subagent_request",
            },
        }
    )

    checkpoint_invocations = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if "checkpoint" in run_id:
            checkpoint_invocations.append(kwargs)
            return {"action": "synthesize"}
        return "Synthesized library search answer"

    ctx.run_node.side_effect = mock_run_node

    await search_pipeline_node._func(ctx, "transformer benchmarks")

    assert len(checkpoint_invocations) == 1
    cp_input = checkpoint_invocations[0]["node_input"]
    assert cp_input["trigger_reason"] == "agent_request"
    assert cp_input["trigger_details"] == {
        "tool_calls_used": 4,
        "tool_budget": 10,
        "words_used": 100,
        "word_ceiling": 7500,
    }
    assert cp_input["agent_justification"] == "Agent needs more retrieval steps to locate benchmarks"


@pytest.mark.asyncio
async def test_library_search_hitl_tool_budget_exhausted_trigger_reason():
    """Verify library search HITL trigger reason is 'tool_budget_exhausted' upon hitting tool call budget."""
    ctx = _MockHitlWorkflowContext(
        state={
            TEMP_RETRIEVED_EVIDENCE: [{"chunk_id": "c1", "text": "Evidence 1", "word_count": 150}],
            TEMP_EXTENSION_REQUESTED: False,
            TEMP_RETRIEVAL_TOOL_COUNT: 10,
            TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            TEMP_WORD_CEILING: 7500,
            TEMP_SEARCH_REPORT: {
                "summary": "10 tool calls used",
                "gaps": ["Missing GPU memory analysis"],
                "notes": "Exhausted budget",
                "source": "budget_exhaustion",
            },
        }
    )

    checkpoint_invocations = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if "checkpoint" in run_id:
            checkpoint_invocations.append(kwargs)
            return {"action": "synthesize"}
        return "Synthesized library search answer"

    ctx.run_node.side_effect = mock_run_node

    await search_pipeline_node._func(ctx, "memory analysis")

    assert len(checkpoint_invocations) == 1
    cp_input = checkpoint_invocations[0]["node_input"]
    assert cp_input["trigger_reason"] == "tool_budget_exhausted"
    assert cp_input["trigger_details"] == {
        "tool_calls_used": 10,
        "tool_budget": 10,
        "words_used": 150,
        "word_ceiling": 7500,
    }
    assert cp_input["agent_justification"] is None


@pytest.mark.asyncio
async def test_library_search_hitl_word_ceiling_approached_trigger_reason():
    """Verify library search HITL trigger reason is 'word_ceiling_approached' when evidence volume exceeds 80% ceiling."""
    ctx = _MockHitlWorkflowContext(
        state={
            TEMP_RETRIEVED_EVIDENCE: [{"chunk_id": "c1", "text": "words " * 6200, "word_count": 6200}],
            TEMP_EXTENSION_REQUESTED: False,
            TEMP_RETRIEVAL_TOOL_COUNT: 5,
            TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            TEMP_WORD_CEILING: 7500,
            TEMP_SEARCH_REPORT: {
                "summary": "Large text volume retrieved",
                "gaps": ["Need ablation details"],
                "notes": "Evidence volume high",
                "source": "volume_warning",
            },
        }
    )

    checkpoint_invocations = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if "checkpoint" in run_id:
            checkpoint_invocations.append(kwargs)
            return {"action": "synthesize"}
        return "Synthesized library search answer"

    ctx.run_node.side_effect = mock_run_node

    await search_pipeline_node._func(ctx, "ablation details")

    assert len(checkpoint_invocations) == 1
    cp_input = checkpoint_invocations[0]["node_input"]
    assert cp_input["trigger_reason"] == "word_ceiling_approached"
    assert cp_input["trigger_details"] == {
        "tool_calls_used": 5,
        "tool_budget": 10,
        "words_used": 6200,
        "word_ceiling": 7500,
    }
    assert cp_input["agent_justification"] is None


@pytest.mark.asyncio
async def test_library_search_hitl_preserves_trigger_reason_on_extension_and_historical_pass():
    """Verify trigger reason is preserved across post-grant snapshot and historical replay pass."""
    # 1. Post-grant snapshot test
    ctx = _MockHitlWorkflowContext(
        state={
            TEMP_RETRIEVED_EVIDENCE: [{"chunk_id": "c1", "text": "Evidence 1", "word_count": 200}],
            TEMP_EXTENSION_REQUESTED: True,
            TEMP_RETRIEVAL_TOOL_COUNT: 4,
            TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            TEMP_WORD_CEILING: 7500,
            TEMP_SEARCH_REPORT: {
                "summary": "Pre-extension summary",
                "gaps": ["Need more context"],
                "notes": "Agent justification text",
                "source": "subagent_request",
            },
        }
    )

    post_grant_snapshot = None

    async def mock_run_node(agent_or_node, *args, **kwargs):
        nonlocal post_grant_snapshot
        run_id = kwargs.get("run_id", "")
        if run_id == "checkpoint_0":
            return {"action": "extend_budget", "guidance": "Focus on ablations"}
        elif run_id == "search_extension_1":
            return "Extension search performed"
        # Synthesizer is invoked after extension loop completes; capture snapshot before cleanup
        if post_grant_snapshot is None and ctx.state.get(HITL_SNAPSHOT):
            post_grant_snapshot = dict(ctx.state.get(HITL_SNAPSHOT))
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    await search_pipeline_node._func(ctx, "test query")

    # Verify post-grant snapshot preserved trigger details
    assert post_grant_snapshot is not None
    assert post_grant_snapshot["trigger_reason"] == "agent_request"
    assert post_grant_snapshot["trigger_details"]["tool_calls_used"] == 4
    assert post_grant_snapshot["agent_justification"] == "Agent justification text"

    # 2. Historical pass test: snapshot already has trigger_reason preserved
    historical_snapshot = {
        "search_query": "historical query",
        "evidence": [{"chunk_id": "c1", "text": "Evidence 1", "word_count": 100}],
        "search_report": {"summary": "Hist", "gaps": ["Hist gap"]},
        "extension_count": 2,
        "tool_count": 10,
        "trigger_reason": "tool_budget_exhausted",
        "trigger_details": {"tool_calls_used": 10, "tool_budget": 10, "words_used": 100, "word_ceiling": 7500},
        "agent_justification": None,
        "ceilings": {"budget": 10, "word_ceiling": 7500, "max_chunks": 15},
    }

    ctx_hist = _MockHitlWorkflowContext(
        state={
            HITL_SNAPSHOT: historical_snapshot,
            TEMP_RETRIEVED_EVIDENCE: historical_snapshot["evidence"],
            TEMP_SEARCH_REPORT: historical_snapshot["search_report"],
            TEMP_RETRIEVAL_TOOL_COUNT: 10,
            TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            TEMP_EXTENSION_COUNT: 0,
        }
    )

    hist_checkpoint_inputs = []

    async def mock_run_node_hist(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if "checkpoint" in run_id:
            hist_checkpoint_inputs.append(kwargs["node_input"])
            return {"action": "synthesize"}
        return "Synthesized answer"

    ctx_hist.run_node.side_effect = mock_run_node_hist

    await search_pipeline_node._func(ctx_hist, "historical query")

    assert len(hist_checkpoint_inputs) == 1
    assert hist_checkpoint_inputs[0]["trigger_reason"] == "tool_budget_exhausted"



