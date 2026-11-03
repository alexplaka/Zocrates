"""Unit tests for Google ADK 2.8+ Context Compaction feature in Zocrates.

Covers:
- TestGroup 1: Configuration & SSOT Backfill (4 tests)
- TestGroup 2: FailOpenLlmEventSummarizer Mechanics (9 tests)
- TestGroup 3: Model Synchronization & Lifecycle (3 tests)
- TestGroup 4: UI Session Formatting & Zero Phantom Bubbles (4 tests)
- TestGroup 5: Rewind Mechanics & History Resurrection (3 tests)
- TestGroup 6: End-to-End ADK Runner Compaction Execution (3 tests)
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.compaction import _safe_token_compaction_split_index
from google.adk.events import Event
from google.adk.events._rewind_events import _apply_rewinds
from google.adk.events.event_actions import EventActions, EventCompaction
from google.adk.flows.llm_flows._content_compaction import _process_compaction_events
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.types import (
    Content,
    FunctionCall,
    FunctionResponse,
    GenerateContentResponseUsageMetadata,
    Part,
)

from zocrates.agents.app import (
    app as default_app,
    coordinator_agent,
    get_compaction_summarizer,
    set_agent_models,
)
from zocrates.agents.common.compaction import (
    ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    FailOpenLlmEventSummarizer,
    build_compaction_llm,
    build_events_compaction_config,
)
from zocrates.config import (
    DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS,
    DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
    DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD,
    DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING,
    DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR,
    DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL,
    DEFAULT_ADAPTIVE_DECISION_MODEL,
    DEFAULT_COMPACTION_SUMMARIZER_MODEL,
    DEFAULT_ENABLE_ADAPTIVE_COMPACTION,
    get_compaction_config,
    is_adaptive_compaction_enabled,
    is_context_compaction_enabled,
    load_zocrates_config,
    save_zocrates_config,
)
from zocrates.utils.session_formatting import format_session_for_ui


# ---------------------------------------------------------------------------
# Helpers & Mock LLMs
# ---------------------------------------------------------------------------

class MockSummarizerLlm(BaseLlm):
    """Mock LLM returning predefined summary content."""

    model: str = "mock-summarizer"
    last_prompt: str | None = None
    calls_count: int = 0
    should_raise: Exception | None = None

    def __init__(self, model: str = "mock-summarizer", **kwargs: Any):
        super().__init__(model=model, **kwargs)
        self.last_prompt = None
        self.calls_count = 0
        self.should_raise = None

    async def generate_content_async(self, req: Any, stream: bool = False):
        self.calls_count += 1
        if self.should_raise:
            raise self.should_raise

        # Extract prompt text
        if req and req.contents and req.contents[0].parts:
            self.last_prompt = req.contents[0].parts[0].text

        summary_text = "Conversation Language: English\n- Summary: Discussed BRCA1 repair mechanisms."
        usage = GenerateContentResponseUsageMetadata(
            prompt_token_count=120,
            candidates_token_count=45,
            total_token_count=165,
        )
        content = Content(role="model", parts=[Part.from_text(text=summary_text)])
        yield LlmResponse(content=content, usage_metadata=usage)


# ===========================================================================
# Group 1: Configuration, SSOT Disk Backfill & API Roundtrip (3 tests)
# ===========================================================================

def test_compaction_default_config_values(tmp_path: Path):
    """Validates that zocrates/rag/config.py supplies all 8 adaptive defaults correctly."""
    assert DEFAULT_ENABLE_ADAPTIVE_COMPACTION is True
    assert DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS == 2
    assert DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS == 4
    assert DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR == 30000
    assert DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING == 100000
    assert DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD == 0.75
    assert DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL == "inception/mercury-decide"
    assert DEFAULT_ADAPTIVE_DECISION_MODEL == "inception/mercury-decide"
    assert DEFAULT_COMPACTION_SUMMARIZER_MODEL == "openrouter/~deepseek/deepseek-flash-latest"

    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        cfg = get_compaction_config(tmp_path)
        assert cfg["enable_adaptive_compaction"] is True
        assert cfg["adaptive_compaction_retention_turns"] == 2
        assert cfg["adaptive_compaction_min_turns"] == 4
        assert cfg["adaptive_compaction_token_floor"] == 30000
        assert cfg["adaptive_compaction_token_ceiling"] == 100000
        assert cfg["adaptive_compaction_threshold"] == 0.75
        assert cfg["adaptive_compaction_decision_model"] == "inception/mercury-decide"
        assert cfg["compaction_summarizer_model"] == "openrouter/~deepseek/deepseek-flash-latest"
        assert is_adaptive_compaction_enabled(tmp_path) is True
        assert is_context_compaction_enabled(tmp_path) is True


def test_compaction_config_disk_backfill_under_file_lock(tmp_path: Path):
    """Initializes a config file on disk without compaction keys and asserts backfill and migration under FileLock."""
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_model": "google/gemini-3.8-flash"}, f)

    loaded = load_zocrates_config(tmp_path)
    assert loaded["enable_adaptive_compaction"] is True
    assert loaded["adaptive_compaction_retention_turns"] == 2
    assert loaded["adaptive_compaction_min_turns"] == 4
    assert loaded["adaptive_compaction_token_floor"] == 30000
    assert loaded["adaptive_compaction_token_ceiling"] == 100000
    assert loaded["adaptive_compaction_threshold"] == 0.75
    assert loaded["adaptive_compaction_decision_model"] == "inception/mercury-decide"
    assert loaded["compaction_summarizer_model"] == "openrouter/~deepseek/deepseek-flash-latest"

    # Verify physically written to disk
    with open(config_path) as f:
        disk_data = json.load(f)
    assert disk_data["enable_adaptive_compaction"] is True
    assert disk_data["adaptive_compaction_retention_turns"] == 2
    assert disk_data["adaptive_compaction_min_turns"] == 4
    assert disk_data["adaptive_compaction_token_floor"] == 30000
    assert disk_data["adaptive_compaction_token_ceiling"] == 100000
    assert disk_data["adaptive_compaction_threshold"] == 0.75
    assert disk_data["adaptive_compaction_decision_model"] == "inception/mercury-decide"
    assert disk_data["compaction_summarizer_model"] == "openrouter/~deepseek/deepseek-flash-latest"

    # Verify migration of deprecated keys on disk:
    legacy_config = {
        "llm_model": "google/gemini-3.8-flash",
        "compaction_interval": 10,
        "compaction_overlap_size": 2,
        "compaction_token_threshold": 80000,
        "compaction_event_retention_size": 2,
        "enable_context_compaction": False,
        "adaptive_decision_model": "typesafe/jev-1.13",
    }
    with open(config_path, "w") as f:
        json.dump(legacy_config, f)

    migrated = load_zocrates_config(tmp_path)
    assert migrated["adaptive_compaction_token_ceiling"] == 80000
    assert migrated["enable_adaptive_compaction"] is False
    assert migrated["adaptive_compaction_decision_model"] == "typesafe/jev-1.13"
    assert "adaptive_decision_model" not in migrated
    for old_key in (
        "compaction_interval",
        "compaction_overlap_size",
        "compaction_token_threshold",
        "compaction_event_retention_size",
        "enable_context_compaction",
    ):
        assert old_key not in migrated

    with open(config_path) as f:
        migrated_disk = json.load(f)
    assert migrated_disk["adaptive_compaction_token_ceiling"] == 80000
    assert migrated_disk["enable_adaptive_compaction"] is False
    assert migrated_disk["adaptive_compaction_decision_model"] == "typesafe/jev-1.13"
    assert "adaptive_decision_model" not in migrated_disk


def test_compaction_settings_in_status_and_llm_config_endpoints(tmp_path):
    """Tests GET /status, GET /config, and POST /config for adaptive compaction parameter roundtrip."""
    from fastapi.testclient import TestClient
    from zocrates.config import get_auth_token
    from zocrates.fast_api_app import app as api_app

    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        client = TestClient(api_app, headers={"Authorization": f"Bearer {get_auth_token()}"})

        # 1. GET /status includes adaptive compaction keys
        resp_status = client.get("/status")
        assert resp_status.status_code == 200
        data_status = resp_status.json()
        assert "enable_adaptive_compaction" in data_status
        assert "adaptive_compaction_retention_turns" in data_status
        assert "adaptive_compaction_min_turns" in data_status
        assert "adaptive_compaction_token_floor" in data_status
        assert "adaptive_compaction_token_ceiling" in data_status
        assert "adaptive_compaction_threshold" in data_status
        assert "adaptive_compaction_decision_model" in data_status
        assert "compaction_summarizer_model" in data_status

        # 2. GET /config includes adaptive compaction keys
        resp_cfg = client.get("/config")
        assert resp_cfg.status_code == 200
        data_cfg = resp_cfg.json()
        assert data_cfg["enable_adaptive_compaction"] is True
        assert data_cfg["adaptive_compaction_retention_turns"] == 2
        assert data_cfg["adaptive_compaction_min_turns"] == 4
        assert data_cfg["adaptive_compaction_token_floor"] == 30000
        assert data_cfg["adaptive_compaction_token_ceiling"] == 100000
        assert data_cfg["adaptive_compaction_threshold"] == 0.75
        assert data_cfg["adaptive_compaction_decision_model"] == "inception/mercury-decide"
        assert data_cfg["compaction_summarizer_model"] == "openrouter/~deepseek/deepseek-flash-latest"

        # 3. POST /config updates adaptive compaction settings
        post_payload = {
            "enable_adaptive_compaction": False,
            "adaptive_compaction_retention_turns": 3,
            "adaptive_compaction_min_turns": 5,
            "adaptive_compaction_token_floor": 25000,
            "adaptive_compaction_token_ceiling": 95000,
            "adaptive_compaction_threshold": 0.80,
            "adaptive_compaction_decision_model": "typesafe/jev-1.13",
            "compaction_summarizer_model": "openrouter/qwen/qwen3.7-flash",
        }
        resp_update = client.post("/config", json=post_payload)
        assert resp_update.status_code == 200

        resp_after = client.get("/config")
        data_after = resp_after.json()
        assert data_after["enable_adaptive_compaction"] is False
        assert data_after["adaptive_compaction_retention_turns"] == 3
        assert data_after["adaptive_compaction_min_turns"] == 5
        assert data_after["adaptive_compaction_token_floor"] == 25000
        assert data_after["adaptive_compaction_token_ceiling"] == 95000
        assert data_after["adaptive_compaction_threshold"] == 0.80
        assert data_after["adaptive_compaction_decision_model"] == "typesafe/jev-1.13"
        assert data_after["compaction_summarizer_model"] == "openrouter/qwen/qwen3.7-flash"

        # Layer 2 validation: Rejects local Laya model with HTTP 400
        resp_bad1 = client.post("/config", json={"adaptive_compaction_decision_model": "local_laya"})
        assert resp_bad1.status_code == 400
        assert "Local Laya is not supported" in resp_bad1.json()["detail"]

        resp_bad2 = client.post("/config", json={"adaptive_compaction_decision_model": "laya"})
        assert resp_bad2.status_code == 400
        assert "Local Laya is not supported" in resp_bad2.json()["detail"]

        # Reset back to defaults
        client.post("/config", json={
            "enable_adaptive_compaction": True,
            "adaptive_compaction_retention_turns": 2,
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_floor": 30000,
            "adaptive_compaction_token_ceiling": 100000,
            "adaptive_compaction_threshold": 0.75,
            "adaptive_compaction_decision_model": "inception/mercury-decide",
            "compaction_summarizer_model": "openrouter/~deepseek/deepseek-flash-latest",
        })


def test_load_zocrates_config_auto_heals_local_laya_model_on_disk(tmp_path):
    """Layer 3: load_zocrates_config auto-heals local Laya model on disk to default remote model."""
    import json
    from zocrates.config import (
        DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL,
        get_zocrates_data_dir,
        load_zocrates_config,
    )

    data_dir = get_zocrates_data_dir(tmp_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = data_dir / "zocrates_config.json"
    cfg_file.write_text(json.dumps({"adaptive_compaction_decision_model": "local_laya"}))

    cfg = load_zocrates_config(tmp_path)
    assert cfg["adaptive_compaction_decision_model"] == DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL

    # Verify written back to disk
    persisted = json.loads(cfg_file.read_text())
    assert persisted["adaptive_compaction_decision_model"] == DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL


# ===========================================================================
# Group 2: FailOpenLlmEventSummarizer Core Mechanics (9 tests)
# ===========================================================================

@pytest.mark.asyncio
async def test_summarizer_excludes_pipeline_scoped_events():
    """Synthetic sequence: prompt sent to LLM contains only unscoped events and 0 scoped pipeline events."""
    mock_llm = MockSummarizerLlm()
    summarizer = FailOpenLlmEventSummarizer(
        llm=mock_llm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )

    # 3 preceding turns
    pre_events = []
    for i in range(1, 4):
        pre_events.append(Event(invocation_id=f"pre_{i}", timestamp=float(i), author="user", content=Content(parts=[Part.from_text(text=f"pre {i}")])))
        pre_events.append(Event(invocation_id=f"pre_{i}", timestamp=float(i) + 0.5, author="coordinator_agent", content=Content(parts=[Part.from_text(text=f"ans {i}")])))

    # 4 unscoped events of inv_1
    u1 = Event(
        invocation_id="inv_1", timestamp=10.0, author="user",
        content=Content(parts=[Part.from_text(text="What is CRISPR-Cas9?")]),
    )
    a1 = Event(
        invocation_id="inv_1", timestamp=11.0, author="coordinator_agent",
        content=Content(parts=[Part(function_call=FunctionCall(name="search_library", id="fc1", args={"query": "CRISPR"}))]),
    )
    # 6 scoped pipeline events
    pipe_events = [
        Event(
            invocation_id="inv_1", timestamp=12.0, author="library_search_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part.from_text(text="INTERNAL_REACT_LOOP_1")]),
        ),
        Event(
            invocation_id="inv_1", timestamp=13.0, author="library_search_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part(function_call=FunctionCall(name="search_corpus", id="sc1", args={}))]),
        ),
        Event(
            invocation_id="inv_1", timestamp=14.0, author="library_search_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part(function_response=FunctionResponse(name="search_corpus", id="sc1", response={"chunk": "RAW CHUNK 42"}))]),
        ),
        Event(
            invocation_id="inv_1", timestamp=15.0, author="library_synthesizer_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part(text="Thinking about Cas9...", thought=True)]),
        ),
        Event(
            invocation_id="inv_1", timestamp=16.0, author="library_synthesizer_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part.from_text(text="INTERNAL_DRAFT_TEXT")]),
        ),
        Event(
            invocation_id="inv_1", timestamp=17.0, author="library_search_agent",
            isolation_scope="search_library_pipeline:fc1",
            content=Content(parts=[Part.from_text(text="SCOPE_CLOSE_MARKER")]),
        ),
    ]
    a2 = Event(
        invocation_id="inv_1", timestamp=18.0, author="coordinator_agent",
        content=Content(parts=[Part(function_response=FunctionResponse(name="search_library", id="fc1", response={"result": "Clean synthesis about CRISPR [1]"}))]),
    )
    a3 = Event(
        invocation_id="inv_1", timestamp=19.0, author="coordinator_agent",
        content=Content(parts=[Part.from_text(text="Here is what I found about CRISPR.")]),
    )

    all_events = pre_events + [u1, a1] + pipe_events + [a2, a3]
    result_event = await summarizer.maybe_summarize_events(events=all_events)

    assert result_event is not None
    assert mock_llm.last_prompt is not None

    # Assert unscoped content present
    assert "What is CRISPR-Cas9?" in mock_llm.last_prompt
    assert "Clean synthesis about CRISPR [1]" in mock_llm.last_prompt

    # Assert scoped subagent content completely excluded
    assert "RAW CHUNK 42" not in mock_llm.last_prompt
    assert "INTERNAL_REACT_LOOP_1" not in mock_llm.last_prompt
    assert "INTERNAL_DRAFT_TEXT" not in mock_llm.last_prompt
    assert "SCOPE_CLOSE_MARKER" not in mock_llm.last_prompt
    assert "search_corpus" not in mock_llm.last_prompt
    assert "Thinking about Cas9..." not in mock_llm.last_prompt


def test_summarizer_formats_markdown_and_filters_thoughts():
    """Verify thoughts are omitted and messages formatted with bold markdown speakers."""
    summarizer = FailOpenLlmEventSummarizer(
        llm=MagicMock(),
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )
    e1 = Event(
        author="user",
        content=Content(parts=[Part.from_text(text="What is ATP?")]),
    )
    e2 = Event(
        author="coordinator_agent",
        content=Content(parts=[
            Part(text="I am thinking about ATP biology...", thought=True),
            Part.from_text(text="ATP is adenosine triphosphate."),
        ]),
    )
    formatted = summarizer._format_events_for_prompt([e1, e2])
    assert "**User:**\nWhat is ATP?" in formatted
    assert "**Assistant:**\nATP is adenosine triphosphate." in formatted
    assert "I am thinking about ATP biology" not in formatted


@pytest.mark.asyncio
async def test_summarizer_preserves_full_invocation_timestamp_span():
    """EventCompaction maintains start_timestamp == events[0].timestamp and end_timestamp == events[-1].timestamp."""
    mock_llm = MockSummarizerLlm()
    summarizer = FailOpenLlmEventSummarizer(
        llm=mock_llm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )

    e1 = Event(invocation_id="inv_1", timestamp=100.5, author="user", content=Content(parts=[Part.from_text(text="Hello")]))
    e2 = Event(invocation_id="inv_1", timestamp=200.5, author="coordinator_agent", content=Content(parts=[Part.from_text(text="Hi")]))
    e3 = Event(invocation_id="inv_2", timestamp=300.5, author="user", content=Content(parts=[Part.from_text(text="How are you?")]))
    e4 = Event(invocation_id="inv_2", timestamp=400.5, author="coordinator_agent", content=Content(parts=[Part.from_text(text="Great")]))

    result = await summarizer.maybe_summarize_events(events=[e1, e2, e3, e4])
    assert result is not None
    assert result.actions.compaction.start_timestamp == 100.5
    assert result.actions.compaction.end_timestamp == 400.5


@pytest.mark.asyncio
async def test_summarizer_trims_incomplete_turn_without_gap_in_coordinator_view():
    """When a trailing incomplete turn is trimmed, end_timestamp anchors to the last summarized turn, leaving 0 gaps."""
    from google.adk.flows.llm_flows._content_compaction import _process_compaction_events

    mock_llm = MockSummarizerLlm()
    summarizer = FailOpenLlmEventSummarizer(
        llm=mock_llm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )

    e1 = Event(invocation_id="inv_1", timestamp=10.0, author="user", content=Content(parts=[Part.from_text(text="T1 User")]))
    e2 = Event(invocation_id="inv_1", timestamp=20.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="T1 Assistant")]))
    e3 = Event(invocation_id="inv_2", timestamp=30.0, author="user", content=Content(parts=[Part.from_text(text="T2 User")]))
    e4 = Event(invocation_id="inv_2", timestamp=40.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="T2 Assistant")]))
    # Trailing incomplete user turn
    e5 = Event(invocation_id="inv_3", timestamp=50.0, author="user", content=Content(parts=[Part.from_text(text="T3 User pending")]))

    comp_event = await summarizer.maybe_summarize_events(events=[e1, e2, e3, e4, e5])
    assert comp_event is not None
    # Crucial: end_timestamp must be 40.0 (the last summarized turn), NOT 50.0!
    assert comp_event.actions.compaction.start_timestamp == 10.0
    assert comp_event.actions.compaction.end_timestamp == 40.0

    # Feed into ADK's prompt assembly
    session_events = [e1, e2, e3, e4, comp_event, e5]
    coordinator_view = _process_compaction_events(session_events, agent_name="coordinator_agent")

    assert len(coordinator_view) == 2
    # 1. Summary of T1 and T2
    assert coordinator_view[0].timestamp == 40.0
    assert coordinator_view[0].author == "coordinator_agent"
    # 2. T3 User prompt preserved verbatim!
    assert coordinator_view[1].timestamp == 50.0
    assert coordinator_view[1].author == "user"
    assert "T3 User pending" in coordinator_view[1].content.parts[0].text


@pytest.mark.asyncio
async def test_summarizer_fail_open_on_llm_exceptions():
    """Catches RuntimeError, TimeoutError, ConnectionError, logs warning, and returns None without raising."""
    for err in (
        RuntimeError("OpenRouter 429 Rate Limit Exceeded"),
        TimeoutError("LLM call timed out after 30s"),
        ConnectionError("Failed to reach OpenRouter gateway"),
    ):
        mock_llm = MockSummarizerLlm()
        mock_llm.should_raise = err
        summarizer = FailOpenLlmEventSummarizer(
            llm=mock_llm,
            prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
        )

        e1 = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Test")]))
        e2 = Event(invocation_id="inv_1", timestamp=2.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="Resp")]))

        result = await summarizer.maybe_summarize_events(events=[e1, e2])
        assert result is None, f"Expected None on {type(err).__name__}, got {result}"


def test_summarizer_tool_content_truncation_ceiling():
    """Tool response of 6,000 characters with trailing # Cited Sources is not truncated at ADK's 2,000 ceiling."""
    mock_llm = MockSummarizerLlm()
    summarizer = FailOpenLlmEventSummarizer(
        llm=mock_llm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )
    assert summarizer._MAX_TOOL_CONTENT_CHARS >= 10000

    long_body = "A" * 5800
    bibliography = "\n# Cited Sources\n- Smith et al. (2021) - BRCA1 mechanisms in homologous recombination"
    full_content = long_body + bibliography

    e = Event(
        invocation_id="inv_1", timestamp=10.0, author="coordinator_agent",
        content=Content(parts=[Part(function_response=FunctionResponse(name="search_library", id="fc1", response={"content": full_content}))]),
    )
    formatted = summarizer._format_events_for_prompt([e])
    assert "# Cited Sources" in formatted
    assert "Smith et al. (2021)" in formatted
    assert "[truncated" not in formatted

    # When content exceeds 30,000 limit, trailing # Cited Sources is still preserved
    giant_content = "B" * 32000 + bibliography
    e_giant = Event(
        invocation_id="inv_2", timestamp=11.0, author="coordinator_agent",
        content=Content(parts=[Part(function_response=FunctionResponse(name="search_library", id="fc2", response={"content": giant_content}))]),
    )
    formatted_giant = summarizer._format_events_for_prompt([e_giant])
    assert "# Cited Sources" in formatted_giant
    assert "Smith et al. (2021)" in formatted_giant
    assert "[truncated" in formatted_giant

    # When online discovery content exceeds 30,000 limit, trailing # Discovered Candidate Papers is preserved
    discovered_section = (
        "\n\n# Discovered Candidate Papers\n"
        "- [1] Alice Smith & Bob Jones (2024). Speculative Drafting. In NeurIPS. DOI: 10.1234/c1\n"
        "  - Rationale: Tree-based draft verification for fast inference."
    )
    giant_online_content = "C" * 32000 + discovered_section
    e_online = Event(
        invocation_id="inv_3", timestamp=12.0, author="coordinator_agent",
        content=Content(parts=[Part(function_response=FunctionResponse(name="search_online_literature", id="fc3", response={"result": giant_online_content}))]),
    )
    formatted_online = summarizer._format_events_for_prompt([e_online])
    assert "# Discovered Candidate Papers" in formatted_online
    assert "Alice Smith & Bob Jones (2024). Speculative Drafting." in formatted_online
    assert "Rationale: Tree-based draft verification for fast inference." in formatted_online
    assert "[truncated" in formatted_online


def test_summarizer_reasoning_effort_suppression():
    """build_compaction_llm() configures llm.extra_body['reasoning']['effort'] == 'none'."""
    cfg = load_zocrates_config()
    llm = build_compaction_llm(cfg)
    assert hasattr(llm, "extra_body")
    assert isinstance(llm.extra_body, dict)
    assert llm.extra_body.get("reasoning", {}).get("effort") == "none"


def test_summarizer_academic_prompt_template_formatting():
    """ZOCRATES_COMPACTION_PROMPT_TEMPLATE formats cleanly and includes all critical academic instructions."""
    formatted = ZOCRATES_COMPACTION_PROMPT_TEMPLATE.format(conversation_history="**User:**\nTest query")
    assert "Sources:" in formatted
    assert "Key Findings & Established Decisions:" in formatted
    assert "Unresolved Questions & Next Steps:" in formatted
    assert "Do NOT use numeric citation brackets" in formatted
    assert "Exclude Internal Mechanics:" in formatted
    assert "<dialogue_history>" in formatted
    assert "**User:**\nTest query" in formatted


# ===========================================================================
# Group 3: Model Synchronization (set_agent_models) (3 tests)
# ===========================================================================

def test_set_agent_models_refreshes_summarizer_llm(tmp_path: Path):
    """set_agent_models(new_model="ollama/llama3.2") refreshes standalone summarizer LLM and keeps events_compaction_config None."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        save_zocrates_config({"compaction_summarizer_model": ""}, zotero_data_dir=tmp_path)
        set_agent_models(new_model="ollama/llama3.2", zotero_data_dir=tmp_path)
        assert default_app.events_compaction_config is None
        summarizer = get_compaction_summarizer()
        assert summarizer is not None
        assert summarizer._llm.model == "ollama_chat/llama3.2"

        # Also verify that when compaction_summarizer_model is configured, it respects the override
        save_zocrates_config(
            {"compaction_summarizer_model": "openrouter/nvidia/nemotron-3.5-lightning:free"},
            zotero_data_dir=tmp_path,
        )
        set_agent_models(new_model="ollama/llama3.2", zotero_data_dir=tmp_path)
        assert summarizer._llm.model == "openrouter/nvidia/nemotron-3.5-lightning:free"

        # Reset
        set_agent_models(zotero_data_dir=tmp_path)


def test_set_agent_models_dict_branch_refreshes_summarizer_llm(tmp_path: Path):
    """set_agent_models({"coordinator": "gpt-4o"}) dictionary branch also refreshes standalone summarizer LLM."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        save_zocrates_config({"compaction_summarizer_model": ""}, zotero_data_dir=tmp_path)
        set_agent_models({"coordinator": "gpt-4o"}, zotero_data_dir=tmp_path)
        assert default_app.events_compaction_config is None
        summarizer = get_compaction_summarizer()
        assert summarizer is not None
        assert "gpt-4o" in summarizer._llm.model

        # Reset
        set_agent_models(zotero_data_dir=tmp_path)


def test_disabled_compaction_returns_none(tmp_path: Path):
    """build_events_compaction_config returns None to deactivate native ADK pre-invocation triggers."""
    cfg = {"enable_adaptive_compaction": False}
    compaction_config = build_events_compaction_config(cfg)
    assert compaction_config is None


def test_compaction_llm_fallback_to_default_summarizer_model(tmp_path: Path):
    """When both compaction_summarizer_model and default_model are empty, falls back to DEFAULT_COMPACTION_SUMMARIZER_MODEL."""
    cfg = {"compaction_summarizer_model": ""}
    llm = build_compaction_llm(cfg, default_model=None)
    assert llm.model == DEFAULT_COMPACTION_SUMMARIZER_MODEL


# ===========================================================================
# Group 4: UI Session Formatting Guard (format_session_for_ui) (4 tests)
# ===========================================================================

def test_format_session_for_ui_filters_compaction_events_completely():
    """Compaction events are filtered out so they never emit phantom empty user messages."""
    t1_u = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Hello")]))
    t1_a = Event(invocation_id="inv_1", timestamp=2.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="Hi there!")]))

    compaction_evt = Event(
        invocation_id="inv_comp", timestamp=3.0, author="user", content=None,
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=2.0,
            compacted_content=Content(parts=[Part.from_text(text="Summary 1-2")]),
        )),
    )

    t2_u = Event(invocation_id="inv_2", timestamp=4.0, author="user", content=Content(parts=[Part.from_text(text="Tell me about BRCA1")]))
    t2_a = Event(invocation_id="inv_2", timestamp=5.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="BRCA1 is a tumor suppressor gene.")]))

    session = MagicMock()
    session.id = "sess_1"
    session.state = {}
    session.events = [t1_u, t1_a, compaction_evt, t2_u, t2_a]

    ui_data = format_session_for_ui(session)
    messages = ui_data["messages"]
    assert len(messages) == 4
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    texts = [m["text"] for m in messages]
    assert texts == ["Hello", "Hi there!", "Tell me about BRCA1", "BRCA1 is a tumor suppressor gene."]
    for m in messages:
        assert m["text"] != ""


def test_format_session_for_ui_multiple_compaction_events():
    """A long session with 3 interleaved compaction events across 12 turns maintains exact 24 messages."""
    events = []
    ts = 1.0
    for i in range(1, 13):
        events.append(Event(invocation_id=f"inv_{i}", timestamp=ts, author="user", content=Content(parts=[Part.from_text(text=f"User Q{i}")])))
        ts += 1.0
        events.append(Event(invocation_id=f"inv_{i}", timestamp=ts, author="coordinator_agent", content=Content(parts=[Part.from_text(text=f"Asst A{i}")])))
        ts += 1.0
        if i in (4, 8, 12):
            events.append(Event(
                invocation_id=f"inv_c_{i}", timestamp=ts, author="user", content=None,
                actions=EventActions(compaction=EventCompaction(
                    start_timestamp=ts - 8.0, end_timestamp=ts - 1.0,
                    compacted_content=Content(parts=[Part.from_text(text=f"Compaction {i}")]),
                )),
            ))
            ts += 1.0

    session = MagicMock()
    session.id = "sess_multi"
    session.state = {}
    session.events = events

    ui_data = format_session_for_ui(session)
    messages = ui_data["messages"]
    assert len(messages) == 24
    for idx, msg in enumerate(messages):
        expected_role = "user" if idx % 2 == 0 else "assistant"
        assert msg["role"] == expected_role


def test_compaction_event_tokens_excluded_from_turn_cost():
    """Compaction event usage_metadata does not contaminate conversational turn cost aggregation."""
    t1_u = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Hello")]))
    t1_a = Event(
        invocation_id="inv_1", timestamp=2.0, author="coordinator_agent",
        content=Content(parts=[Part.from_text(text="Hi!")]),
        usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=50, total_token_count=150),
    )

    compaction_evt = Event(
        invocation_id="inv_comp", timestamp=3.0, author="user", content=None,
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=2.0,
            compacted_content=Content(parts=[Part.from_text(text="Summary")]),
        )),
        usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=50000, candidates_token_count=1000, total_token_count=51000),
    )

    session = MagicMock()
    session.id = "sess_cost"
    session.state = {}
    session.events = [t1_u, t1_a, compaction_evt]

    ui_data = format_session_for_ui(session)
    assert len(ui_data["messages"]) == 2
    # Ensure total tokens match Turn 1 (150 tokens), not 51150
    assert ui_data["total_tokens"] == 150
    assert ui_data["session_metadata"]["total_tokens"] == 150
    asst_msg = ui_data["messages"][1]
    usage = asst_msg.get("usage", {})
    assert usage.get("total_tokens") == 150


def test_format_session_for_ui_unconditional_filtering():
    """Compaction events are filtered even when has_rewinds is False."""
    session = MagicMock()
    session.id = "sess_no_rewind"
    session.state = {}
    u = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Question")]))
    a = Event(invocation_id="inv_1", timestamp=2.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="Answer")]))
    c = Event(
        invocation_id="inv_c", timestamp=3.0, author="user", content=None,
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=2.0,
            compacted_content=Content(parts=[Part.from_text(text="Summary")]),
        )),
    )
    session.events = [u, a, c]

    ui_data = format_session_for_ui(session)
    assert len(ui_data["messages"]) == 2


# ===========================================================================
# Group 5: Rewind Mechanics & History Resurrection (3 tests)
# ===========================================================================

def test_rewind_after_compacted_window_preserves_compaction():
    """Rewinding before Turn 4 preserves Compaction C1 (Turns 1-2) and Turn 3."""
    t1_u = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Q1")]))
    t1_a = Event(invocation_id="inv_1", timestamp=2.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="A1")]))
    t2_u = Event(invocation_id="inv_2", timestamp=3.0, author="user", content=Content(parts=[Part.from_text(text="Q2")]))
    t2_a = Event(invocation_id="inv_2", timestamp=4.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="A2")]))

    c1 = Event(
        invocation_id="inv_c1", timestamp=5.0, author="user",
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=4.0,
            compacted_content=Content(parts=[Part.from_text(text="Summary 1-2")]),
        )),
    )

    t3_u = Event(invocation_id="inv_3", timestamp=6.0, author="user", content=Content(parts=[Part.from_text(text="Q3")]))
    t3_a = Event(invocation_id="inv_3", timestamp=7.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="A3")]))
    t4_u = Event(invocation_id="inv_4", timestamp=8.0, author="user", content=Content(parts=[Part.from_text(text="Q4")]))

    rewind_evt = Event(
        invocation_id="inv_rewind", timestamp=9.0, author="user",
        actions=EventActions(rewind_before_invocation_id="inv_4"),
    )

    surviving = _apply_rewinds([t1_u, t1_a, t2_u, t2_a, c1, t3_u, t3_a, t4_u, rewind_evt])
    # Surviving raw events: t1, t2, c1, t3
    assert t4_u not in surviving
    assert c1 in surviving

    # Prompt builder replaces compacted range 1-2 with Summary 1-2
    compacted_history = _process_compaction_events(surviving, agent_name="coordinator_agent")
    assert len(compacted_history) == 3  # [Summary 1-2, t3_u, t3_a]
    assert compacted_history[0].content.parts[0].text == "Summary 1-2"
    assert compacted_history[1].content.parts[0].text == "Q3"


def test_rewind_into_compacted_window_resurrects_raw_events():
    """Rewinding before Turn 2 eliminates Compaction C1 and resurrects Turn 1 in its raw un-compacted form."""
    t1_u = Event(invocation_id="inv_1", timestamp=1.0, author="user", content=Content(parts=[Part.from_text(text="Q1")]))
    t1_a = Event(invocation_id="inv_1", timestamp=2.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="A1")]))
    t2_u = Event(invocation_id="inv_2", timestamp=3.0, author="user", content=Content(parts=[Part.from_text(text="Q2")]))
    t2_a = Event(invocation_id="inv_2", timestamp=4.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="A2")]))

    c1 = Event(
        invocation_id="inv_c1", timestamp=5.0, author="user",
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=4.0,
            compacted_content=Content(parts=[Part.from_text(text="Summary 1-2")]),
        )),
    )
    t3_u = Event(invocation_id="inv_3", timestamp=6.0, author="user", content=Content(parts=[Part.from_text(text="Q3")]))

    rewind_evt = Event(
        invocation_id="inv_rewind", timestamp=7.0, author="user",
        actions=EventActions(rewind_before_invocation_id="inv_2"),
    )

    surviving = _apply_rewinds([t1_u, t1_a, t2_u, t2_a, c1, t3_u, rewind_evt])
    # c1 was appended after inv_2, so it is dropped. inv_2 and inv_3 are dropped.
    assert c1 not in surviving
    assert t2_u not in surviving
    assert surviving == [t1_u, t1_a]


@pytest.mark.asyncio
async def test_rewind_state_delta_reversal():
    """Runner._compute_state_delta_for_rewind accurately undoes state changes when rewinding past compaction."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="zocrates")
    runner = Runner(app=default_app, session_service=session_service)

    # Initial state delta on Turn 1
    e1 = Event(
        invocation_id="inv_1", timestamp=1.0, author="coordinator_agent",
        actions=EventActions(state_delta={"research_turn_history": ["turn1_entry"]}),
    )
    # Compaction event has no state delta
    e2 = Event(
        invocation_id="inv_comp", timestamp=2.0, author="user",
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=1.0, end_timestamp=1.0, compacted_content=Content(),
        )),
    )
    # Turn 2 state delta
    e3 = Event(
        invocation_id="inv_2", timestamp=3.0, author="coordinator_agent",
        actions=EventActions(state_delta={"research_turn_history": ["turn1_entry", "turn2_entry"], "active_topic": "oncology"}),
    )

    session.events = [e1, e2, e3]
    session.state = {"research_turn_history": ["turn1_entry", "turn2_entry"], "active_topic": "oncology"}

    # Rewind before inv_2 (index 2)
    rewind_delta = await runner._compute_state_delta_for_rewind(session, rewind_event_index=2)
    assert rewind_delta["research_turn_history"] == ["turn1_entry"]
    assert rewind_delta["active_topic"] is None


# ===========================================================================
# Group 6: ADK Compaction Safeguards & Isolation (4 tests)
# ===========================================================================

def test_open_tool_call_mid_turn_blocks_in_progress_compaction():
    """_safe_token_compaction_split_index shifts split earlier to avoid orphaning tool calls."""
    e1 = Event(
        invocation_id="inv_1", timestamp=1.0, author="coordinator_agent",
        content=Content(parts=[Part(function_call=FunctionCall(name="search_library", id="fc1", args={}))]),
    )
    e2 = Event(
        invocation_id="inv_1", timestamp=2.0, author="coordinator_agent",
        content=Content(parts=[Part(function_response=FunctionResponse(name="search_library", id="fc1", response={"res": "ok"}))]),
    )

    # When event_retention_size=1, the split index cannot cut between e1 (call) and e2 (response)
    split_idx = _safe_token_compaction_split_index(
        candidate_events=[e1, e2],
        event_retention_size=1,
    )
    assert split_idx == 0


@pytest.mark.asyncio
async def test_compaction_telemetry_span_emission():
    """Verify that maybe_summarize_events starts an OpenTelemetry span with Phoenix attributes."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry import trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    mock_llm = MagicMock()
    mock_llm.model = "test-summarizer-model"
    mock_response = MagicMock()
    mock_response.content = Content(role="model", parts=[Part.from_text(text="Compact summary text")])
    mock_response.usage_metadata = GenerateContentResponseUsageMetadata(
        prompt_token_count=120,
        candidates_token_count=45,
        total_token_count=165,
    )

    async def mock_gen(*args, **kwargs):
        yield mock_response

    mock_llm.generate_content_async = mock_gen

    summarizer = FailOpenLlmEventSummarizer(
        llm=mock_llm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )

    events = [
        Event(author="user", invocation_id="inv_1", timestamp=1.0, content=Content(parts=[Part.from_text(text="Hello")])),
        Event(author="coordinator_agent", invocation_id="inv_1", timestamp=2.0, content=Content(parts=[Part.from_text(text="Hi there")])),
    ]

    with patch.object(trace, "get_tracer", side_effect=provider.get_tracer):
        event = await summarizer.maybe_summarize_events(events=events)

    assert event is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "compact_session_history"
    assert span.attributes["openinference.span.kind"] == "LLM"
    assert span.attributes["llm.model_name"] == "test-summarizer-model"
    assert span.attributes["output.value"] == "Compact summary text"
    assert span.attributes["llm.token_count.prompt"] == 120
    assert span.attributes["llm.token_count.completion"] == 45
    assert span.attributes["llm.token_count.total"] == 165
    assert span.attributes["compaction.total_events"] == 2


@pytest.mark.asyncio
async def test_compaction_subagent_isolation_blocks_pipeline_agents():
    """Verify that patch_compaction_subagent_isolation blocks compaction from executing for subagents."""
    import google.adk.flows.llm_flows.compaction as adk_compaction
    from zocrates.agents.common.adk_compat import patch_compaction_subagent_isolation

    patch_compaction_subagent_isolation()

    # 1. Subagent with isolation_scope
    mock_ctx1 = MagicMock()
    mock_ctx1.token_compaction_checked = False
    mock_ctx1.events_compaction_config = MagicMock()
    mock_ctx1.isolation_scope = "search_library_pipeline:fc1"
    mock_ctx1.agent = MagicMock()
    mock_ctx1.agent.name = "library_search_agent"

    events1 = []
    async for event in adk_compaction.request_processor.run_async(mock_ctx1, MagicMock()):
        events1.append(event)
    assert len(events1) == 0
    assert mock_ctx1.token_compaction_checked is False

    # 2. Subagent without isolation_scope but non-coordinator name
    mock_ctx2 = MagicMock()
    mock_ctx2.token_compaction_checked = False
    mock_ctx2.events_compaction_config = MagicMock()
    mock_ctx2.isolation_scope = None
    mock_ctx2.agent = MagicMock()
    mock_ctx2.agent.name = "online_search_agent"

    events2 = []
    async for event in adk_compaction.request_processor.run_async(mock_ctx2, MagicMock()):
        events2.append(event)
    assert len(events2) == 0
    assert mock_ctx2.token_compaction_checked is False


@pytest.mark.asyncio
async def test_post_invocation_token_compaction_skipped_if_already_compacted_this_turn():
    """Verify that _run_compaction_for_token_threshold is skipped when compaction ran pre-invocation in the same turn."""
    import google.adk.apps.compaction as adk_app_compaction
    from google.adk.sessions import Session, InMemorySessionService
    from zocrates.agents.common.adk_compat import patch_compaction_subagent_isolation

    patch_compaction_subagent_isolation()

    # Session with: Turn 1, then Turn 2 User, Turn 2 Compaction Event, Turn 2 Assistant response
    session = Session(id="test_post_skip_session", app_name="zocrates", user_id="test_user")
    session_service = InMemorySessionService()

    e1 = Event(invocation_id="inv_1", timestamp=10.0, author="user", content=Content(parts=[Part.from_text(text="T1")]))
    e2 = Event(invocation_id="inv_1", timestamp=20.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="R1")]))
    e3 = Event(invocation_id="inv_2", timestamp=30.0, author="user", content=Content(parts=[Part.from_text(text="T2")]))
    comp = Event(
        invocation_id="comp_inv", timestamp=35.0, author="user",
        actions=EventActions(compaction=EventCompaction(start_timestamp=10.0, end_timestamp=20.0, compacted_content=Content(parts=[Part.from_text(text="Sum")]))),
    )
    e4 = Event(invocation_id="inv_2", timestamp=40.0, author="coordinator_agent", content=Content(parts=[Part.from_text(text="R2")]))

    session.events = [e1, e2, e3, comp, e4]

    # Attempt to run post-invocation token compaction
    mock_app = MagicMock()
    mock_app.events_compaction_config = MagicMock()
    result = await adk_app_compaction._run_compaction_for_token_threshold(mock_app, session, session_service)

    # Must be skipped immediately without calling summarizer!
    assert result is False


@pytest.mark.asyncio
async def test_runner_post_invocation_compaction_skips_on_hitl_and_suppresses_token_compaction():
    """Verify that Runner._run_post_invocation_compaction skips on HITL interrupt and enforces skip_token_compaction=True."""
    from google.adk.runners import Runner
    from google.adk.sessions import Session, InMemorySessionService
    from zocrates.agents.common.adk_compat import patch_compaction_subagent_isolation

    patch_compaction_subagent_isolation()

    # 1. Interrupted by HITL budget checkpoint
    session_hitl = Session(id="test_hitl_session", app_name="zocrates", user_id="test_user")
    session_hitl.events = [
        Event(author="user", timestamp=10.0, content=Content(parts=[Part.from_text(text="hi")])),
        Event(author="search_budget_checkpoint", timestamp=20.0, content=Content(parts=[Part(function_call=FunctionCall(name="search_budget_checkpoint", id="c1"))])),
    ]

    runner = Runner(app=MagicMock(), session_service=InMemorySessionService())
    runner.app.events_compaction_config = MagicMock()

    # Calling post-invocation compaction on HITL-paused session must exit immediately without running compaction
    with patch("google.adk.apps.compaction._run_compaction_for_sliding_window") as mock_sliding:
        await runner._run_post_invocation_compaction(session=session_hitl, skip_token_compaction=False)
        mock_sliding.assert_not_called()

    # 2. Completed normal turn: must pass skip_token_compaction=True to underlying compactor
    session_normal = Session(id="test_normal_session", app_name="zocrates", user_id="test_user")
    session_normal.events = [
        Event(author="user", timestamp=10.0, content=Content(parts=[Part.from_text(text="hi")])),
        Event(author="coordinator_agent", timestamp=20.0, content=Content(parts=[Part.from_text(text="done")])),
    ]

    with patch("google.adk.apps.compaction._run_compaction_for_sliding_window") as mock_sliding:
        # Mock async generator
        async def empty_gen(*args, **kwargs):
            if False:
                yield
        mock_sliding.side_effect = empty_gen
        await runner._run_post_invocation_compaction(session=session_normal, skip_token_compaction=False)
        mock_sliding.assert_called_once()
        _, kwargs = mock_sliding.call_args
        assert kwargs["skip_token_compaction"] is True


