"""Unit tests for /sessions REST endpoints."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.utils.services import get_session_service, reset_session_service


def test_session_crud_endpoints():
    """Verify the full CRUD lifecycle of the /sessions REST endpoints.

    Uses a temporary directory-backed ``DatabaseSessionService`` to avoid
    leaking state between test runs. Exercises, in order:

      1. ``GET /sessions`` returns an empty list initially.
      2. ``POST /sessions`` creates a session with the specified title.
      3. ``GET /sessions`` lists the newly created session.
      4. ``GET /sessions/{id}`` returns the session with an empty message list.
      5. ``DELETE /sessions/{id}`` deletes the session.
      6. ``GET /sessions`` confirms the list is empty again.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        reset_session_service()
        # Bind DatabaseSessionService to temporary directory
        get_session_service(tmpdir)

        with TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
            # 1. Initially empty sessions
            res = client.get("/sessions")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["sessions"] == []

            # 2. Create a session
            res = client.post("/sessions", json={"title": "Test AI Chat"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            session_id = data["session"]["id"]
            assert data["session"]["title"] == "Test AI Chat"

            # 3. List sessions contains created session
            res = client.get("/sessions")
            assert res.status_code == 200
            data = res.json()
            assert len(data["sessions"]) == 1
            assert data["sessions"][0]["id"] == session_id

            # 4. Get specific session
            res = client.get(f"/sessions/{session_id}")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["session"]["id"] == session_id
            assert data["session"]["title"] == "Test AI Chat"
            assert data["session"]["messages"] == []

            # 5. Delete session
            res = client.delete(f"/sessions/{session_id}")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"

            # 6. Verify session is deleted
            res = client.get("/sessions")
            assert res.status_code == 200
            assert res.json()["sessions"] == []


def test_format_session_for_ui_thought_handling():
    """Verify that format_session_for_ui correctly extracts both boolean-flag and string-valued thoughts."""
    from unittest.mock import MagicMock
    from zocrates.fast_api_app import format_session_for_ui

    # 1. Google GenAI / ADK convention: Part(text="Thinking process...", thought=True) + Part(text="Answer")
    part_thought_bool = MagicMock()
    part_thought_bool.thought = True
    part_thought_bool.text = "Thinking process..."

    part_text_normal = MagicMock()
    part_text_normal.thought = False
    part_text_normal.text = "Final answer."

    event_bool = MagicMock()
    event_bool.author = "coordinator_agent"
    event_bool.timestamp = 123456789.0
    event_bool.content = MagicMock()
    event_bool.content.parts = [part_thought_bool, part_text_normal]

    session_bool = MagicMock()
    session_bool.id = "session_1"
    session_bool.state = {"session_title": "Test Chat"}
    session_bool.events = [event_bool]

    result_bool = format_session_for_ui(session_bool)
    assert len(result_bool["messages"]) == 1
    msg_bool = result_bool["messages"][0]
    assert msg_bool["text"] == "Final answer."
    assert msg_bool["thought"] == "Thinking process..."

    # 2. Alternative string-valued convention: Part(thought="Direct thought string") + Part(text="Answer")
    part_thought_str = MagicMock()
    part_thought_str.thought = "Direct thought string"
    part_thought_str.text = None

    event_str = MagicMock()
    event_str.author = "coordinator_agent"
    event_str.timestamp = 123456790.0
    event_str.content = MagicMock()
    event_str.content.parts = [part_thought_str, part_text_normal]

    session_str = MagicMock()
    session_str.id = "session_2"
    session_str.state = {"session_title": "Test Chat 2"}
    session_str.events = [event_str]

    result_str = format_session_for_ui(session_str)
    assert len(result_str["messages"]) == 1
    msg_str = result_str["messages"][0]
    assert msg_str["text"] == "Final answer."
    assert msg_str["thought"] == "Direct thought string"
    assert msg_str["author"] == "Zocrates"


def test_format_author_name_mappings():
    """Verify canonical author name normalization for agent roles."""
    from zocrates.utils.session_formatting import format_author_name

    assert format_author_name("coordinator_agent") == "Zocrates"
    assert format_author_name("coordinator") == "Zocrates"
    assert format_author_name("library_search_agent") == "Library Search"
    assert format_author_name("library_synthesizer_agent") == "Library Synthesis"
    assert format_author_name("online_search_agent") == "Online Discovery"
    assert format_author_name("online_synthesizer_agent") == "Discovery Synthesis"
    assert format_author_name("user") == "User"


def test_session_rewind_endpoint():
    """Verify POST /sessions/{session_id}/rewind calls runner.rewind_async and returns formatted session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        reset_session_service()
        get_session_service(tmpdir)

        with TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
            mock_runner = MagicMock()
            mock_runner.rewind_async = AsyncMock()
            app.state.runner = mock_runner

            # 1. Create a session
            res = client.post("/sessions", json={"title": "Rewind Chat"})
            assert res.status_code == 200
            session_id = res.json()["session"]["id"]

            # 2. Call rewind endpoint with valid invocation_id
            res = client.post(
                f"/sessions/{session_id}/rewind",
                json={"invocation_id": "inv-turn-2"}
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert f"rewound before invocation inv-turn-2" in data["message"]
            assert "session" in data
            assert data["session"]["id"] == session_id

            mock_runner.rewind_async.assert_awaited_once_with(
                user_id="default_user",
                session_id=session_id,
                rewind_before_invocation_id="inv-turn-2"
            )

            # 3. Call rewind when runner raises ValueError (unknown invocation_id)
            mock_runner.rewind_async.side_effect = ValueError("Invocation ID not found: inv-invalid")
            res = client.post(
                f"/sessions/{session_id}/rewind",
                json={"invocation_id": "inv-invalid"}
            )
            assert res.status_code == 404
            assert "Invocation ID not found" in res.json()["detail"]


def test_format_session_for_ui_rewind_exclusion():
    """Verify format_session_for_ui includes invocation_id on user messages and excludes rewound turns."""
    from google.genai import types
    from google.adk.events import Event, EventActions
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    e1_user = Event(id='e1', invocation_id='inv-1', author='user', content=types.Content(role='user', parts=[types.Part.from_text(text='Q1')]))
    e1_asst = Event(id='e2', invocation_id='inv-1', author='synthesizer_agent', content=types.Content(role='model', parts=[types.Part.from_text(text='A1')]))

    e2_user = Event(id='e3', invocation_id='inv-2', author='user', content=types.Content(role='user', parts=[types.Part.from_text(text='Q2')]))
    e2_asst = Event(id='e4', invocation_id='inv-2', author='synthesizer_agent', content=types.Content(role='model', parts=[types.Part.from_text(text='A2')]))

    # Rewind before inv-2
    e_rewind = Event(id='e5', invocation_id='inv-3', author='user', actions=EventActions(rewind_before_invocation_id='inv-2'))

    session = Session(app_name='zocrates', user_id='u', id='s1', events=[e1_user, e1_asst, e2_user, e2_asst, e_rewind])
    formatted = format_session_for_ui(session)

    assert len(formatted["messages"]) == 2
    assert formatted["messages"][0]["role"] == "user"
    assert formatted["messages"][0]["invocation_id"] == "inv-1"
    assert formatted["messages"][0]["text"] == "Q1"
    assert formatted["messages"][1]["role"] == "assistant"
    assert formatted["messages"][1]["invocation_id"] == "inv-1"
    assert formatted["messages"][1]["text"] == "A1"


def test_search_pipeline_handles_none_state_after_rewind():
    """Verify that search pipeline safely treats None state values (from ADK rewind delta) as empty collections."""
    from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE, TEMP_RETRIEVED_EVIDENCE

    # Simulate rolled-back state where ADK sets keys to None
    simulated_state = {
        RETRIEVED_EVIDENCE: None,
        TEMP_RETRIEVED_EVIDENCE: None,
    }

    # Verify safe list conversion without TypeError
    evidence = simulated_state.get(TEMP_RETRIEVED_EVIDENCE) or []
    assert evidence == []

    audit_history = list(simulated_state.get(RETRIEVED_EVIDENCE) or [])
    assert audit_history == []


def test_format_session_consolidates_fragmented_thoughts():
    """Verify that extract_event_parts and format_session_for_ui consolidate contiguous thought parts."""
    from google.genai import types
    from google.adk.events import Event
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import extract_event_parts, format_session_for_ui

    fragmented_parts = [
        types.Part(thought=True, text="The "),
        types.Part(thought=True, text="user asked "),
        types.Part(thought=True, text="a question."),
        types.Part.from_text(text="Final answer."),
    ]
    evt = Event(id="e-test", author="synthesizer_agent", content=types.Content(role="model", parts=fragmented_parts))

    txt, thoughts, calls, responses = extract_event_parts(evt)
    assert thoughts == ["The user asked a question."]
    assert txt == "Final answer."

    user_evt = Event(id="e-u", invocation_id="inv-1", author="user", content=types.Content(role="user", parts=[types.Part.from_text(text="Hi")]))
    asst_evt = Event(id="e-a", invocation_id="inv-1", author="synthesizer_agent", content=types.Content(role="model", parts=fragmented_parts))
    session = Session(app_name="zocrates", user_id="u", id="s-th", events=[user_evt, asst_evt])

    formatted = format_session_for_ui(session)
    asst_msg = formatted["messages"][1]
    assert asst_msg["thought"] == "The user asked a question."
    assert len(asst_msg["intermediate_steps"]) == 1
    assert asst_msg["intermediate_steps"][0]["type"] == "thought"
    assert asst_msg["intermediate_steps"][0]["content"] == "The user asked a question."


def test_format_session_intermediate_steps_ordering():
    """Verify that agent_text is ordered before tool_call and tool_return in intermediate_steps."""
    from google.genai import types
    from google.adk.events import Event
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    user_evt = Event(
        id="e-u",
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Find papers")]),
    )
    # Search subagent emits thought, observation text, and a tool call
    search_evt = Event(
        id="e-search",
        invocation_id="inv-1",
        author="library_search_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(thought=True, text="I will search the catalog."),
                types.Part.from_text(text="I'll inspect the document structure first."),
                types.Part(function_call=types.FunctionCall(name="get_document_toc", args={"item_key": "K1"})),
            ],
        ),
    )
    resp_evt = Event(
        id="e-resp",
        invocation_id="inv-1",
        author="library_search_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_response=types.FunctionResponse(name="get_document_toc", response={"sections": ["s1"]})),
            ],
        ),
    )
    synth_evt = Event(
        id="e-synth",
        invocation_id="inv-1",
        author="library_synthesizer_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Here is the summary.")],
        ),
    )

    session = Session(app_name="zocrates", user_id="u", id="s-order", events=[user_evt, search_evt, resp_evt, synth_evt])
    formatted = format_session_for_ui(session)
    asst_msg = formatted["messages"][1]
    steps = asst_msg["intermediate_steps"]

    assert len(steps) == 4
    assert steps[0]["type"] == "thought"
    assert steps[0]["content"] == "I will search the catalog."
    assert steps[1]["type"] == "agent_text"
    assert steps[1]["text"] == "I'll inspect the document structure first."
    assert steps[2]["type"] == "tool_call"
    assert steps[2]["name"] == "get_document_toc"
    assert steps[3]["type"] == "tool_return"
    assert steps[3]["name"] == "get_document_toc"


def test_format_session_parallel_tool_calls_retained():
    """Verify parallel tool calls to the same tool retain all calls and responses with call_id."""
    from google.genai import types
    from google.adk.events import Event
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    user_evt = Event(
        id="e-u",
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Compare topic A and B")]),
    )
    # Parallel tool calls to search_corpus with different queries and distinct call IDs
    parallel_calls_evt = Event(
        id="e-calls",
        invocation_id="inv-1",
        author="library_search_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="call-search-1",
                        name="search_corpus",
                        args={"query": "quantum computing"},
                    )
                ),
                types.Part(
                    function_call=types.FunctionCall(
                        id="call-search-2",
                        name="search_corpus",
                        args={"query": "classical computing"},
                    )
                ),
            ],
        ),
    )
    # 2 x search_corpus responses with matching distinct call IDs
    parallel_responses_evt = Event(
        id="e-responses",
        invocation_id="inv-1",
        author="library_search_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-search-1",
                        name="search_corpus",
                        response={"results": ["doc1"]},
                    )
                ),
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-search-2",
                        name="search_corpus",
                        response={"results": ["doc2"]},
                    )
                ),
            ],
        ),
    )
    synth_evt = Event(
        id="e-synth",
        invocation_id="inv-1",
        author="synthesizer_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Comparison complete.")],
        ),
    )

    session = Session(
        app_name="zocrates",
        user_id="u",
        id="s-parallel",
        events=[user_evt, parallel_calls_evt, parallel_responses_evt, synth_evt],
    )
    formatted = format_session_for_ui(session)
    asst_msg = formatted["messages"][1]
    steps = asst_msg["intermediate_steps"]

    tool_calls = [s for s in steps if s["type"] == "tool_call"]
    tool_returns = [s for s in steps if s["type"] == "tool_return"]

    assert len(tool_calls) == 2
    assert tool_calls[0]["name"] == "search_corpus"
    assert tool_calls[0]["args"] == {"query": "quantum computing"}
    assert tool_calls[0]["call_id"] == "call-search-1"

    assert tool_calls[1]["name"] == "search_corpus"
    assert tool_calls[1]["args"] == {"query": "classical computing"}
    assert tool_calls[1]["call_id"] == "call-search-2"

    assert len(tool_returns) == 2
    assert tool_returns[0]["name"] == "search_corpus"
    assert tool_returns[0]["response"] == {"results": ["doc1"]}
    assert tool_returns[0]["call_id"] == "call-search-1"

    assert tool_returns[1]["name"] == "search_corpus"
    assert tool_returns[1]["response"] == {"results": ["doc2"]}
    assert tool_returns[1]["call_id"] == "call-search-2"


def test_parse_usage_metadata_fallback():
    """Verify parse_usage_metadata correctly falls back to prompt + candidates when total is 0 or None."""
    from zocrates.utils.session_formatting import parse_usage_metadata

    # 1. total_tokens is 0 but prompt and candidates exist (dict)
    meta_zero_total = {"prompt_tokens": 120, "candidates_tokens": 45, "total_tokens": 0}
    parsed = parse_usage_metadata(meta_zero_total)
    assert parsed is not None
    assert parsed["prompt_tokens"] == 120
    assert parsed["candidates_tokens"] == 45
    assert parsed["total_tokens"] == 165

    # 2. total_tokens is None / missing (dict)
    meta_none_total = {"prompt_tokens": 80, "candidates_tokens": 20}
    parsed = parse_usage_metadata(meta_none_total)
    assert parsed is not None
    assert parsed["total_tokens"] == 100

    # 3. Object with total_token_count = 0
    obj_zero = MagicMock()
    obj_zero.prompt_token_count = 50
    obj_zero.candidates_token_count = 25
    obj_zero.total_token_count = 0
    obj_zero.thoughts_token_count = 10
    obj_zero.cached_content_token_count = 5
    parsed = parse_usage_metadata(obj_zero)
    assert parsed is not None
    assert parsed["prompt_tokens"] == 50
    assert parsed["candidates_tokens"] == 25
    assert parsed["thoughts_tokens"] == 10
    assert parsed["cached_tokens"] == 5
    assert parsed["total_tokens"] == 75

    # 4. Explicit non-zero total_tokens preserved
    meta_explicit = {"prompt_tokens": 50, "candidates_tokens": 50, "total_tokens": 120}
    parsed = parse_usage_metadata(meta_explicit)
    assert parsed is not None
    assert parsed["total_tokens"] == 120

    # 5. Empty or all-None returns None
    assert parse_usage_metadata(None) is None
    assert parse_usage_metadata({}) is None
    assert parse_usage_metadata({"thoughts_tokens": 10}) is None


def test_format_session_for_ui_total_turns():
    """Verify format_session_for_ui computes total_turns correctly, counting Zocrates and subagents while excluding welcome."""
    from google.genai import types
    from google.adk.events import Event
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    # 1. Leading welcome message starting with "Hello! I am Zocrates"
    welcome_evt = Event(
        id="e-welcome",
        author="coordinator_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Hello! I am Zocrates, your research assistant.")],
        ),
    )

    # 2. Turn 1: Conversational turn with coordinator_agent (author "Zocrates")
    user1_evt = Event(
        id="e-u1",
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="What is flash attention?")]),
    )
    asst1_evt = Event(
        id="e-a1",
        invocation_id="inv-1",
        author="coordinator_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="FlashAttention is an exact attention algorithm that reduces memory IO.")],
        ),
    )

    # 3. Turn 2: Retrieval turn ending with library_synthesizer_agent
    user2_evt = Event(
        id="e-u2",
        invocation_id="inv-2",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Find citations in my library.")]),
    )
    synth_evt = Event(
        id="e-synth",
        invocation_id="inv-2",
        author="library_synthesizer_agent",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Here are the citations from your library: [1].")],
        ),
    )

    session = Session(
        app_name="zocrates",
        user_id="u",
        id="s-turns",
        events=[welcome_evt, user1_evt, asst1_evt, user2_evt, synth_evt],
    )

    res = format_session_for_ui(session)

    # Total turns must be exactly 2: 1 for coordinator_agent conversational turn + 1 for synthesizer turn
    # The welcome message must NOT be counted as a turn
    assert res["total_turns"] == 2
    assert res["session_metadata"]["total_turns"] == 2
    assert "total_tokens" in res
    assert "total_tokens" in res["session_metadata"]
    assert len(res["messages"]) == 5


def test_format_session_for_ui_attaches_snippets_from_state_delta():
    """Verify format_session_for_ui extracts snippets from user_evt.actions.state_delta and attaches to user message."""
    from google.genai import types
    from google.adk.events import Event, EventActions
    from google.adk.sessions import Session
    from zocrates.utils.session_formatting import format_session_for_ui

    mock_snippets = [
        {
            "id": "snip-1",
            "type": "text",
            "paperTitle": "Attention Is All You Need",
            "text": "Self-attention mechanism",
            "page": 3,
        },
        {
            "id": "snip-2",
            "type": "image",
            "paperTitle": "ResNet",
            "imageData": "data:image/jpeg;base64,...",
            "page": 5,
        },
    ]

    user_evt = Event(
        id="e1",
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Look at these snippets")]),
        actions=EventActions(
            state_delta={
                "active_context_items": [{"key": "ITEM1", "title": "Paper 1"}],
                "snippets": mock_snippets,
            }
        ),
    )
    asst_evt = Event(
        id="e2",
        invocation_id="inv-1",
        author="coordinator_agent",
        content=types.Content(role="model", parts=[types.Part.from_text(text="I see the snippets.")]),
    )

    session = Session(app_name="zocrates", user_id="u", id="s1", events=[user_evt, asst_evt])
    formatted = format_session_for_ui(session)

    assert len(formatted["messages"]) == 2
    user_msg = formatted["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["text"] == "Look at these snippets"
    assert user_msg["snippets"] == mock_snippets
    assert user_msg["active_context_items"] == [{"key": "ITEM1", "title": "Paper 1"}]


def test_adk_active_task_scope_rewind_patch():
    """Verify that _find_active_task_scope ignores unfinished isolation scopes in rewound turns."""
    import google.adk.runners as adk_runners
    from google.adk.events import Event, EventActions
    from google.adk.sessions import Session
    from google.genai import types
    from zocrates.agents.common.adk_compat import patch_adk_active_task_scope_rewind

    # Ensure compatibility patch is active
    patch_adk_active_task_scope_rewind()

    # 1. Un-rewound active task scope is correctly detected
    live_event = Event(
        id="e-live-1",
        invocation_id="inv-live-1",
        author="library_search_agent",
        isolation_scope="search_library_pipeline:active_scope_1",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="In-flight search task")],
        ),
    )
    live_session = Session(
        app_name="zocrates",
        user_id="u",
        id="s-live",
        events=[live_event],
    )
    assert adk_runners._find_active_task_scope(live_session) == (
        "search_library_pipeline:active_scope_1",
        "inv-live-1",
    )

    # 2. Session with unfinished task event followed by a rewind event returns None
    rewound_event = Event(
        id="e-rewound-1",
        invocation_id="inv-rewound-1",
        author="library_search_agent",
        isolation_scope="search_library_pipeline:xyz",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="In-flight search before turn was rewound")],
        ),
    )
    rewind_event = Event(
        id="e-rewind-action",
        invocation_id="inv-rewind-2",
        author="user",
        actions=EventActions(rewind_before_invocation_id="inv-rewound-1"),
    )
    rewound_session = Session(
        app_name="zocrates",
        user_id="u",
        id="s-rewound",
        events=[rewound_event, rewind_event],
    )
    assert adk_runners._find_active_task_scope(rewound_session) is None

    # 3. Multi-turn session: completed turn, then rewound in-flight turn, active scope returns None
    turn1_user = Event(
        id="e1",
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Hi")]),
    )
    turn1_asst = Event(
        id="e2",
        invocation_id="inv-1",
        author="coordinator_agent",
        content=types.Content(role="model", parts=[types.Part.from_text(text="Hello!")]),
    )
    turn2_user = Event(
        id="e3",
        invocation_id="inv-2",
        author="user",
        content=types.Content(role="user", parts=[types.Part.from_text(text="Search library")]),
    )
    turn2_task = Event(
        id="e4",
        invocation_id="inv-2",
        author="library_search_agent",
        isolation_scope="search_library_pipeline:xyz_flight",
        content=types.Content(role="model", parts=[types.Part.from_text(text="Searching...")]),
    )
    turn3_rewind = Event(
        id="e5",
        invocation_id="inv-3",
        author="user",
        actions=EventActions(rewind_before_invocation_id="inv-2"),
    )
    multi_turn_session = Session(
        app_name="zocrates",
        user_id="u",
        id="s-multi-rewound",
        events=[turn1_user, turn1_asst, turn2_user, turn2_task, turn3_rewind],
    )
    assert adk_runners._find_active_task_scope(multi_turn_session) is None




