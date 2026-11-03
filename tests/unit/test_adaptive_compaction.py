"""Phase 4 Comprehensive Unit Test Suite for Post-Turn Decision-Model Adaptive Compaction.

Implements all 17 required unit tests for PLN-ADAPTIVE-COMPACTION-ADK28-V4:
1. test_envelope_gate_skips_when_few_turns: Asserts 0 decision calls when completed_turns < min_turns.
2. test_envelope_gate_disambiguation_clamping: Asserts min_turns <= K clamps effective_min_turns = K + 1 with warning.
3. test_envelope_gate_skips_when_tokens_below_floor: Asserts 0 decision calls when tokens < 30k.
4. test_hard_backstop_forces_compress: Asserts decision model is bypassed and COMPRESS fired when tokens >= 100k.
5. test_decision_model_compress_verdict: Asserts COMPRESS fires when p >= 0.75.
6. test_decision_model_continue_verdict: Asserts no compaction when p < 0.75.
7. test_retention_turns_preserves_latest_k_turns_verbatim: Verifies Turns N-1 and N have ts > end_timestamp and remain verbatim in coordinator prompt.
8. test_rolling_consolidation_subsumes_earlier_compaction: Verifies second compaction consolidates prior summary and new turns, subsuming earlier event.
9. test_slicing_applies_rewinds_correctly: Verifies rewound invocations are excluded from compaction summaries.
10. test_set_agent_models_refreshes_standalone_summarizer: Verifies model updates correctly propagate to the adaptive summarizer.
11. test_pre_invocation_compaction_is_guaranteed_noop: Asserts events_compaction_config is None and pre-invocation never stalls (0ms TTFT).
12. test_hitl_interruption_immunity: Asserts paused turns (search_budget_checkpoint) never trigger adaptive evaluation.
13. test_disk_migration_idempotency_and_purge: Asserts deprecated keys are cleanly purged on disk without loss of custom thresholds.
14. test_coordinator_prompt_tokens_ignores_subagent_events: Asserts token counting filters out isolation_scope is not None subagent events and strictly targets coordinator events.
15. test_stale_session_concurrency_fail_open: Asserts concurrent modification (StaleSessionError) is caught and dropped safely with zero session corruption.
16. test_telemetry_spans_emitted_for_decision_and_summarizer: Asserts compaction_decision_probe (EVALUATOR) and compact_session_history (LLM) spans are emitted with complete OpenInference attributes and token counts.
17. test_adaptive_compaction_end_to_end_multiturn_simulation: End-to-end multi-turn simulation checking Turn 1 -> Turn 2 -> Turn 3 -> Turn 4 -> Turn 5 -> Turn 6, envelope gating, compaction event generation, subsumption, and context preservation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.events import Event
from google.adk.events.event_actions import EventActions, EventCompaction, ToolConfirmation
from google.adk.flows.llm_flows._content_compaction import _process_compaction_events
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.genai.types import (
    Content,
    FunctionCall,
    GenerateContentResponseUsageMetadata,
    Part,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from zocrates.agents.app import (
    app as default_app,
    get_compaction_summarizer,
    set_agent_models,
)
from zocrates.agents.common.adaptive_decision import (
    count_completed_turns_since_last_compaction,
    evaluate_adaptive_compaction_rubric,
    extract_decision_state,
    get_coordinator_prompt_tokens,
    should_trigger_adaptive_compaction,
)
from zocrates.agents.common.compaction import (
    FailOpenLlmEventSummarizer,
    ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    build_events_compaction_config,
    compact_session_adaptive,
    run_adaptive_post_invocation_compaction,
)
from zocrates.config import (
    load_zocrates_config,
    save_zocrates_config,
)


# ---------------------------------------------------------------------------
# Test Helpers & Mock Summarizer
# ---------------------------------------------------------------------------

class MockSummarizerLlm(BaseLlm):
    """Mock LLM for compaction tests capturing prompts and yielding deterministic summaries."""

    model: str = "mock-compactor-model"
    summary_text: str = "Mock academic compaction summary."
    last_prompt: str | None = None
    calls: int = 0
    should_raise: Any = None

    def __init__(
        self,
        model: str = "mock-compactor-model",
        summary_text: str = "Mock academic compaction summary.",
        **kwargs: Any,
    ):
        super().__init__(
            model=model,
            summary_text=summary_text,
            last_prompt=None,
            calls=0,
            should_raise=None,
            **kwargs,
        )

    async def generate_content_async(self, req: Any, stream: bool = False):
        self.calls += 1
        if self.should_raise:
            raise self.should_raise

        if req and req.contents and req.contents[0].parts:
            self.last_prompt = req.contents[0].parts[0].text

        usage = GenerateContentResponseUsageMetadata(
            prompt_token_count=150,
            candidates_token_count=50,
            total_token_count=200,
        )
        content = Content(role="model", parts=[Part.from_text(text=self.summary_text)])
        yield LlmResponse(content=content, usage_metadata=usage)


def make_turn(
    turn_idx: int,
    q: str,
    a: str,
    start_ts: float,
    prompt_tokens: int | None = None,
) -> list[Event]:
    """Helper creating a 2-event completed turn with optional usage_metadata."""
    inv_id = f"inv_{turn_idx}"
    usage = None
    if prompt_tokens is not None:
        usage = GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=100,
            total_token_count=prompt_tokens + 100,
        )
    return [
        Event(
            author="user",
            invocation_id=inv_id,
            timestamp=start_ts,
            content=Content(parts=[Part.from_text(text=q)]),
        ),
        Event(
            author="coordinator_agent",
            invocation_id=inv_id,
            timestamp=start_ts + 1.0,
            content=Content(parts=[Part.from_text(text=a)]),
            usage_metadata=usage,
        ),
    ]


def build_mock_summarizer(
    summary_text: str = "Mock academic compaction summary.",
) -> FailOpenLlmEventSummarizer:
    """Create a FailOpenLlmEventSummarizer wrapped around MockSummarizerLlm."""
    return FailOpenLlmEventSummarizer(
        llm=MockSummarizerLlm(summary_text=summary_text),
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )


# ===========================================================================
# Phase 4 Test Suite (17 Tests)
# ===========================================================================

@pytest.mark.asyncio
async def test_envelope_gate_skips_when_few_turns():
    """1. Asserts 0 decision calls when completed_turns < min_turns."""
    events = []
    # 2 completed turns, tokens satisfying token floor (35000 >= 30000)
    events.extend(make_turn(1, "What causes capacity fade?", "SEI layer growth and plating.", 10.0, prompt_tokens=35000))
    events.extend(make_turn(2, "Do silicon anodes suffer from this?", "Silicon faces particle pulverization.", 20.0, prompt_tokens=35000))

    session = Session(id="s_few_turns", app_name="zocrates", user_id="u1")
    session.events = events

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
    }

    assert count_completed_turns_since_last_compaction(session) == 2

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric") as mock_probe:
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is False
        assert "turn_count_below_min" in reason
        assert "2 < 4" in reason
        # Zero decision model calls made
        mock_probe.assert_not_called()


@pytest.mark.asyncio
async def test_envelope_gate_disambiguation_clamping(caplog: pytest.LogCaptureFixture):
    """2. Asserts min_turns <= K automatically clamps effective_min_turns = K + 1 with a warning."""
    # N = 2 completed turns
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0, prompt_tokens=35000))
    events.extend(make_turn(2, "Q2", "A2", 20.0, prompt_tokens=35000))

    session = Session(id="s_clamp", app_name="zocrates", user_id="u1")
    session.events = events

    # Misconfiguration: min_turns = 1 <= retention_turns = 2
    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 1,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
    }

    with caplog.at_level(logging.WARNING):
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is False
        # Clamped effective_min_turns is K + 1 = 3
        assert "turn_count_below_min (2 < 3)" in reason

    # Verify warning logged
    warning_logged = any(
        "adaptive_compaction_min_turns (1) must be strictly greater than retention_turns (2)" in record.message
        and "clamping effective_min_turns to 3" in record.message
        for record in caplog.records
    )
    assert warning_logged, f"Expected disambiguation clamping warning not found in logs: {[r.message for r in caplog.records]}"

    # Add Turn 3: completed turns reaches effective_min_turns (3 == 3)
    events.extend(make_turn(3, "Q3", "A3", 30.0, prompt_tokens=35000))
    session.events = events

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric", return_value=0.85):
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is True


@pytest.mark.asyncio
async def test_envelope_gate_skips_when_tokens_below_floor():
    """3. Asserts 0 decision calls when tokens < 30k."""
    events = []
    # 4 completed turns (satisfying turn count), but prompt tokens = 15000 (< 30000 floor)
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10), prompt_tokens=15000))

    session = Session(id="s_low_tokens", app_name="zocrates", user_id="u1")
    session.events = events

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
    }

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric") as mock_probe:
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is False
        assert "tokens_below_floor" in reason
        assert "15000 < 30000" in reason
        mock_probe.assert_not_called()


@pytest.mark.asyncio
async def test_hard_backstop_forces_compress():
    """4. Asserts decision model is bypassed and COMPRESS fired when tokens >= 100k."""
    events = []
    # 4 completed turns, prompt tokens = 105000 (>= 100000 ceiling)
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10), prompt_tokens=105000))

    session = Session(id="s_ceiling", app_name="zocrates", user_id="u1")
    session.events = events

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
        "adaptive_compaction_token_ceiling": 100000,
    }

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric") as mock_probe:
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is True
        assert "token_ceiling_exceeded" in reason
        assert "105000 >= 100000" in reason
        # Decision model is bypassed entirely on hard backstop
        mock_probe.assert_not_called()


@pytest.mark.asyncio
async def test_decision_model_compress_verdict():
    """5. Asserts COMPRESS fires when p >= 0.75."""
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10), prompt_tokens=40000))

    session = Session(id="s_compress_verdict", app_name="zocrates", user_id="u1")
    session.events = events

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
        "adaptive_compaction_token_ceiling": 100000,
        "adaptive_compaction_threshold": 0.75,
    }

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric", return_value=0.85) as mock_probe:
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is True
        assert "decision_probe_triggered" in reason
        assert "p=0.850 >= 0.75" in reason
        mock_probe.assert_called_once()

    # Direct evaluate_adaptive_compaction_rubric roundtrip check
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"answers": {"compaction_warranted": {"noul": 0.85}}}

    state = extract_decision_state(session)
    with patch("requests.post", return_value=mock_resp):
        prob = evaluate_adaptive_compaction_rubric(state, cfg, api_key="test-key")
        assert prob == 0.85


@pytest.mark.asyncio
async def test_decision_model_continue_verdict():
    """6. Asserts no compaction when p < 0.75."""
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10), prompt_tokens=40000))

    session = Session(id="s_continue_verdict", app_name="zocrates", user_id="u1")
    session.events = events

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
        "adaptive_compaction_token_ceiling": 100000,
        "adaptive_compaction_threshold": 0.75,
    }

    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric", return_value=0.42) as mock_probe:
        should_compress, reason = should_trigger_adaptive_compaction(session, cfg)
        assert should_compress is False
        assert "decision_probe_declined" in reason
        assert "p=0.420 < 0.75" in reason
        mock_probe.assert_called_once()

    # Direct evaluate_adaptive_compaction_rubric roundtrip check
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"answers": {"compaction_warranted": {"noul": 0.42}}}

    state = extract_decision_state(session)
    with patch("requests.post", return_value=mock_resp):
        prob = evaluate_adaptive_compaction_rubric(state, cfg, api_key="test-key")
        assert prob == 0.42


@pytest.mark.asyncio
async def test_retention_turns_preserves_latest_k_turns_verbatim():
    """7. Verifies Turns N-1 and N have ts > end_timestamp and remain verbatim in coordinator prompt."""
    events = []
    events.extend(make_turn(1, "Question 1", "Answer 1", 10.0))
    events.extend(make_turn(2, "Question 2", "Answer 2", 20.0))
    events.extend(make_turn(3, "Question 3", "Answer 3", 30.0))
    events.extend(make_turn(4, "Question 4", "Answer 4", 40.0))

    session = Session(id="s_retention", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Summary of Turns 1 and 2")
    cfg = {"adaptive_compaction_retention_turns": 2}

    compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert compaction_event is not None
    comp = compaction_event.actions.compaction
    assert comp is not None

    # Exact boundary timestamps: covers Turns 1 and 2
    assert comp.start_timestamp == 10.0
    assert comp.end_timestamp == 21.0

    # Retained turns (Turns 3 and 4) have timestamps strictly greater than end_timestamp
    for turn_3_4_evt in events[4:]:
        assert turn_3_4_evt.timestamp > comp.end_timestamp

    # Verify summarizer prompt only received candidate turns (Turns 1 and 2)
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    assert "Question 1" in prompt
    assert "Answer 1" in prompt
    assert "Question 2" in prompt
    assert "Answer 2" in prompt
    assert "Question 3" not in prompt
    assert "Question 4" not in prompt

    # Verify ADK prompt reconstruction via _process_compaction_events:
    # Turns 3 and 4 are preserved 100% verbatim
    processed = _process_compaction_events([*events, compaction_event])
    processed_texts = [
        p.content.parts[0].text for p in processed if p.content and p.content.parts
    ]
    assert processed_texts[0] == "Summary of Turns 1 and 2"
    assert "Question 3" in processed_texts
    assert "Answer 3" in processed_texts
    assert "Question 4" in processed_texts
    assert "Answer 4" in processed_texts


@pytest.mark.asyncio
async def test_rolling_consolidation_subsumes_earlier_compaction():
    """8. Verifies second compaction consolidates prior summary and new turns, subsuming earlier event."""
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    events.extend(make_turn(2, "Q2", "A2", 20.0))

    comp1 = Event(
        author="user",
        invocation_id="inv_comp1",
        timestamp=22.0,
        actions=EventActions(
            compaction=EventCompaction(
                start_timestamp=10.0,
                end_timestamp=21.0,
                compacted_content=Content(role="model", parts=[Part.from_text(text="First summary covering Turns 1-2")]),
            )
        ),
    )
    events.append(comp1)

    events.extend(make_turn(3, "Q3", "A3", 30.0))
    events.extend(make_turn(4, "Q4", "A4", 40.0))
    events.extend(make_turn(5, "Q5", "A5", 50.0))
    events.extend(make_turn(6, "Q6", "A6", 60.0))

    session = Session(id="s_rolling", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Consolidated summary covering Turns 1-4")
    cfg = {"adaptive_compaction_retention_turns": 2}

    comp2 = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert comp2 is not None
    c2 = comp2.actions.compaction
    assert c2 is not None

    # Preserves comp1.start_timestamp anchor (10.0)
    assert c2.start_timestamp == 10.0
    # Extends to Turn 4's final timestamp (41.0)
    assert c2.end_timestamp == 41.0

    # Rolling prompt includes prior summary AND new completed turns (Turns 3 and 4)
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    assert "### Existing Conversation Summary" in prompt
    assert "First summary covering Turns 1-2" in prompt
    assert "### New Completed Turns to Integrate" in prompt
    assert "Q3" in prompt
    assert "A3" in prompt
    assert "Q4" in prompt
    assert "A4" in prompt
    assert "Q5" not in prompt
    assert "Q6" not in prompt

    # ADK subsumption check: comp2 subsumes comp1 completely
    processed = _process_compaction_events([*events, comp2])
    processed_texts = [
        p.content.parts[0].text for p in processed if p.content and p.content.parts
    ]
    assert processed_texts[0] == "Consolidated summary covering Turns 1-4"
    assert "Q5" in processed_texts
    assert "A5" in processed_texts
    assert "Q6" in processed_texts
    assert "A6" in processed_texts


@pytest.mark.asyncio
async def test_slicing_applies_rewinds_correctly():
    """9. Verifies rewound invocations are excluded from compaction summaries."""
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    events.extend(make_turn(2, "Q2", "A2", 20.0))
    events.extend(make_turn(3, "Q3_BAD", "A3_BAD", 30.0))
    events.extend(make_turn(4, "Q4_BAD", "A4_BAD", 40.0))

    # User rewinds before inv_3
    rewind_event = Event(
        author="user",
        invocation_id="inv_rewind",
        timestamp=45.0,
        actions=EventActions(rewind_before_invocation_id="inv_3"),
    )
    events.append(rewind_event)

    events.extend(make_turn(5, "Q5_GOOD", "A5_GOOD", 50.0))
    events.extend(make_turn(6, "Q6_GOOD", "A6_GOOD", 60.0))

    session = Session(id="s_rewind", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Summary after rewind")
    cfg = {"adaptive_compaction_retention_turns": 2}

    compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert compaction_event is not None

    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    # Bad rewound turns must not appear in compaction prompt
    assert "BAD" not in prompt
    assert "Q3_BAD" not in prompt
    assert "Q4_BAD" not in prompt
    assert "Q1" in prompt
    assert "Q2" in prompt


def test_set_agent_models_refreshes_standalone_summarizer(tmp_path: Path):
    """10. Verifies model updates correctly propagate to the adaptive summarizer."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        save_zocrates_config({"compaction_summarizer_model": ""}, zotero_data_dir=tmp_path)
        set_agent_models(new_model="ollama/llama3.2", zotero_data_dir=tmp_path)

        assert default_app.events_compaction_config is None
        summarizer = get_compaction_summarizer()
        assert summarizer is not None
        assert summarizer._llm.model == "ollama_chat/llama3.2"

        # Explicit compaction_summarizer_model override is respected
        save_zocrates_config(
            {"compaction_summarizer_model": "openrouter/nvidia/nemotron-3.5-lightning:free"},
            zotero_data_dir=tmp_path,
        )
        set_agent_models(new_model="ollama/llama3.2", zotero_data_dir=tmp_path)
        assert summarizer._llm.model == "openrouter/nvidia/nemotron-3.5-lightning:free"

        # Dictionary branch
        save_zocrates_config({"compaction_summarizer_model": ""}, zotero_data_dir=tmp_path)
        set_agent_models({"coordinator": "custom/gpt-4o"}, zotero_data_dir=tmp_path)
        assert default_app.events_compaction_config is None
        assert "gpt-4o" in summarizer._llm.model

        # Reset back to default
        set_agent_models(zotero_data_dir=tmp_path)


def test_pre_invocation_compaction_is_guaranteed_noop():
    """11. Asserts events_compaction_config is None and pre-invocation never stalls (0ms TTFT)."""
    assert default_app.events_compaction_config is None

    # Check build_events_compaction_config returns None unconditionally
    assert build_events_compaction_config({"enable_adaptive_compaction": True}) is None
    assert build_events_compaction_config(None) is None

    # Verify that a runner constructed with default_app has events_compaction_config = None
    runner = Runner(app=default_app, session_service=InMemorySessionService())
    assert runner.app.events_compaction_config is None


@pytest.mark.asyncio
async def test_hitl_interruption_immunity():
    """12. Asserts paused turns (search_budget_checkpoint) never trigger adaptive evaluation."""
    # 1. Open function call (search_budget_checkpoint)
    events_open_call = [
        Event(author="user", timestamp=10.0, invocation_id="inv_1", content=Content(parts=[Part.from_text(text="deep literature search")])),
        Event(
            author="coordinator_agent",
            timestamp=20.0,
            invocation_id="inv_1",
            content=Content(parts=[Part(function_call=FunctionCall(name="search_budget_checkpoint", id="c1"))]),
        ),
    ]
    session_hitl = Session(id="s_hitl", app_name="zocrates", user_id="u1")
    session_hitl.events = events_open_call

    runner = Runner(app=MagicMock(), session_service=InMemorySessionService())
    with patch("zocrates.agents.common.adaptive_decision.should_trigger_adaptive_compaction") as mock_should:
        result = await run_adaptive_post_invocation_compaction(runner, session_hitl, cfg={"enable_adaptive_compaction": True})
        assert result is False
        mock_should.assert_not_called()

    # 2. Awaiting tool confirmations
    events_conf = [
        Event(author="user", timestamp=10.0, invocation_id="inv_1", content=Content(parts=[Part.from_text(text="delete item")])),
        Event(
            author="coordinator_agent",
            timestamp=20.0,
            invocation_id="inv_1",
            actions=EventActions(requested_tool_confirmations={"c1": ToolConfirmation(hint="confirm")}),
        ),
    ]
    session_conf = Session(id="s_conf", app_name="zocrates", user_id="u1")
    session_conf.events = events_conf

    with patch("zocrates.agents.common.adaptive_decision.should_trigger_adaptive_compaction") as mock_should:
        result_conf = await run_adaptive_post_invocation_compaction(runner, session_conf, cfg={"enable_adaptive_compaction": True})
        assert result_conf is False
        mock_should.assert_not_called()


def test_disk_migration_idempotency_and_purge(tmp_path: Path):
    """13. Asserts deprecated keys are cleanly purged on disk without loss of custom thresholds."""
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    legacy_payload = {
        "llm_model": "openrouter/anthropic/claude-3.5-sonnet",
        "compaction_interval": 12,
        "compaction_overlap_size": 4,
        "compaction_token_threshold": 92000,
        "compaction_event_retention_size": 3,
        "enable_context_compaction": True,
        "adaptive_compaction_threshold": 0.88,
        "adaptive_compaction_retention_turns": 3,
    }
    with open(config_path, "w") as f:
        json.dump(legacy_payload, f)

    # First load: performs atomic disk migration under FileLock
    loaded = load_zocrates_config(tmp_path)
    assert loaded["adaptive_compaction_token_ceiling"] == 92000  # migrated from compaction_token_threshold
    assert loaded["enable_adaptive_compaction"] is True          # migrated from enable_context_compaction
    assert loaded["adaptive_compaction_threshold"] == 0.88       # custom threshold preserved
    assert loaded["adaptive_compaction_retention_turns"] == 3    # custom retention preserved

    # Deprecated keys must be removed from memory
    for deprecated_key in (
        "compaction_interval",
        "compaction_overlap_size",
        "compaction_token_threshold",
        "compaction_event_retention_size",
        "enable_context_compaction",
    ):
        assert deprecated_key not in loaded

    # Verify physical disk state: deprecated keys are purged
    with open(config_path) as f:
        disk_data = json.load(f)
    assert disk_data["adaptive_compaction_token_ceiling"] == 92000
    assert disk_data["enable_adaptive_compaction"] is True
    assert disk_data["adaptive_compaction_threshold"] == 0.88
    for deprecated_key in (
        "compaction_interval",
        "compaction_overlap_size",
        "compaction_token_threshold",
        "compaction_event_retention_size",
        "enable_context_compaction",
    ):
        assert deprecated_key not in disk_data

    # Idempotent second load: does not re-mutate or corrupt
    reloaded = load_zocrates_config(tmp_path)
    assert reloaded["adaptive_compaction_token_ceiling"] == 92000
    assert reloaded["enable_adaptive_compaction"] is True
    assert reloaded["adaptive_compaction_threshold"] == 0.88


def test_coordinator_prompt_tokens_ignores_subagent_events():
    """14. Asserts token counting filters out isolation_scope is not None subagent events and strictly targets coordinator events."""
    events = [
        # Turn 1: coordinator
        Event(
            author="user",
            invocation_id="inv_1",
            timestamp=10.0,
            content=Content(parts=[Part.from_text(text="Search library")]),
        ),
        Event(
            author="coordinator_agent",
            invocation_id="inv_1",
            timestamp=11.0,
            isolation_scope=None,
            content=Content(parts=[Part.from_text(text="I'll search for that.")]),
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=12000),
        ),
        # Internal subagent pipeline event with massive token count
        Event(
            author="library_search_agent",
            invocation_id="inv_1",
            timestamp=12.0,
            isolation_scope="search_pipeline:fc_lib",
            content=Content(parts=[Part.from_text(text="Searching chunks...")]),
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=98000),
        ),
        # Subagent tool call with even higher token count
        Event(
            author="library_synthesizer_agent",
            invocation_id="inv_1",
            timestamp=13.0,
            isolation_scope="search_pipeline:fc_lib",
            content=Content(parts=[Part.from_text(text="Synthesizing chunks...")]),
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=110000),
        ),
    ]

    # Must return 12000, NOT 98000 or 110000!
    assert get_coordinator_prompt_tokens(events) == 12000

    # Add Turn 2: coordinator event with 38000 tokens
    events.extend([
        Event(
            author="user",
            invocation_id="inv_2",
            timestamp=20.0,
            content=Content(parts=[Part.from_text(text="Followup question")]),
        ),
        Event(
            author="coordinator_agent",
            invocation_id="inv_2",
            timestamp=21.0,
            isolation_scope=None,
            content=Content(parts=[Part.from_text(text="Here is the synthesis.")]),
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=38000),
        ),
        # Subagent in turn 2
        Event(
            author="online_search_agent",
            invocation_id="inv_2",
            timestamp=22.0,
            isolation_scope="search_pipeline:fc_online",
            content=Content(parts=[Part.from_text(text="Online retrieval...")]),
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=85000),
        ),
    ])

    # Latest coordinator prompt token count is 38000
    assert get_coordinator_prompt_tokens(events) == 38000


@pytest.mark.asyncio
async def test_stale_session_concurrency_fail_open():
    """15. Asserts concurrent modification (StaleSessionError) is caught and dropped safely with zero session corruption."""
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10), prompt_tokens=35000))

    session = Session(id="s_stale_err", app_name="zocrates", user_id="u1")
    session.events = events

    # Mock session service where append_event raises StaleSessionError
    mock_service = MagicMock()
    mock_service.append_event = AsyncMock(side_effect=Exception("StaleSessionError: revision marker conflict"))

    runner = Runner(app=MagicMock(), session_service=mock_service)
    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_token_floor": 30000,
    }

    with patch("zocrates.agents.common.compaction.should_trigger_adaptive_compaction", return_value=(True, "forced_compress")), \
         patch("zocrates.agents.common.compaction.compact_session_adaptive", return_value=Event(author="user", invocation_id="c_evt")):
        result = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)

    # Caught defensively, returns False, zero exception raised
    assert result is False


@pytest.mark.asyncio
async def test_telemetry_spans_emitted_for_decision_and_summarizer():
    """16. Asserts compaction_decision_probe (EVALUATOR) and compact_session_history (LLM) spans
    are emitted with complete OpenInference attributes and token counts.
    """
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10)))

    session = Session(id="s_telemetry_all", app_name="zocrates", user_id="u1")
    session.events = events

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # 1. Decision probe span
    cfg = {
        "adaptive_compaction_decision_model": "inception/mercury-decide:free",
        "adaptive_compaction_threshold": 0.75,
        "adaptive_compaction_retention_turns": 2,
    }
    state = extract_decision_state(session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"answers": {"compaction_warranted": {"noul": 0.82}}}

    with patch.object(trace, "get_tracer", side_effect=provider.get_tracer), \
         patch("requests.post", return_value=mock_resp):
        prob = evaluate_adaptive_compaction_rubric(
            state,
            cfg,
            api_key="test-openrouter-key",
            metadata={"session_id": "s_telemetry_all", "completed_turns": 4, "coordinator_prompt_tokens": 38400, "threshold": 0.75},
        )
        assert prob == 0.82

    # 2. Summarizer span
    summarizer = build_mock_summarizer("Telemetry test summary")
    with patch.object(trace, "get_tracer", side_effect=provider.get_tracer):
        compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
        assert compaction_event is not None

    spans = exporter.get_finished_spans()
    assert len(spans) == 2

    # Verify decision probe span
    probe_span = next(s for s in spans if s.name == "compaction_decision_probe")
    assert probe_span.attributes["openinference.span.kind"] == "EVALUATOR"
    assert probe_span.attributes["compaction.decision.provider"] == "openrouter"
    assert probe_span.attributes["compaction.decision.model"] == "inception/mercury-decide:free"
    assert probe_span.attributes["compaction.decision.probability"] == 0.82
    assert probe_span.attributes["compaction.decision.threshold"] == 0.75
    assert probe_span.attributes["compaction.decision.verdict"] == "COMPRESS"
    assert probe_span.attributes["compaction.decision.status"] == "success"
    assert probe_span.attributes["input.mime_type"] == "application/json"
    assert probe_span.attributes["output.mime_type"] == "application/json"
    assert "metadata" in probe_span.attributes

    # Verify summarizer span
    summarizer_span = next(s for s in spans if s.name == "compact_session_history")
    assert summarizer_span.attributes["openinference.span.kind"] == "LLM"
    assert summarizer_span.attributes["llm.model_name"] == "mock-compactor-model"
    assert summarizer_span.attributes["gen_ai.request.model"] == "mock-compactor-model"
    assert "temperature" in summarizer_span.attributes["llm.invocation_parameters"]
    assert summarizer_span.attributes["input.mime_type"] == "text/plain"
    assert summarizer_span.attributes["output.mime_type"] == "text/plain"
    assert summarizer_span.attributes["output.value"] == "Telemetry test summary"
    assert summarizer_span.attributes["llm.token_count.prompt"] == 150
    assert summarizer_span.attributes["llm.token_count.completion"] == 50
    assert summarizer_span.attributes["llm.token_count.total"] == 200
    assert summarizer_span.attributes["compaction.summarize.start_timestamp"] == 10.0
    assert summarizer_span.attributes["compaction.summarize.end_timestamp"] == 21.0
    assert summarizer_span.attributes["compaction.summarize.slice_turns"] == 2
    assert summarizer_span.attributes["compaction.summarize.retained_turns"] == 2


@pytest.mark.asyncio
async def test_adaptive_compaction_end_to_end_multiturn_simulation():
    """17. End-to-end multi-turn simulation checking Turn 1 -> Turn 2 -> Turn 3 -> Turn 4 -> Turn 5,
    envelope gating, compaction event generation, subsumption, and context preservation.
    """
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="zocrates", user_id="u_sim", session_id="s_multiturn_sim"
    )

    summarizer = build_mock_summarizer("Summary of Turns 1 and 2")
    runner = Runner(app=MagicMock(), session_service=session_service)

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_token_floor": 30000,
        "adaptive_compaction_token_ceiling": 100000,
        "adaptive_compaction_threshold": 0.75,
    }

    # Turn 1: 5000 tokens
    for evt in make_turn(1, "What is solid-electrolyte interphase?", "SEI is a passivation layer.", 10.0, prompt_tokens=5000):
        await session_service.append_event(session=session, event=evt)
    r1 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)
    assert r1 is False
    assert len(session.events) == 2

    # Turn 2: 12000 tokens
    for evt in make_turn(2, "How does SEI degrade capacity?", "Consumes active lithium.", 20.0, prompt_tokens=12000):
        await session_service.append_event(session=session, event=evt)
    r2 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)
    assert r2 is False
    assert len(session.events) == 4

    # Turn 3: 22000 tokens
    for evt in make_turn(3, "Does temperature affect SEI growth?", "Higher temperatures accelerate growth.", 30.0, prompt_tokens=22000):
        await session_service.append_event(session=session, event=evt)
    r3 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)
    assert r3 is False
    assert len(session.events) == 6

    # Turn 4: 35000 tokens (>= 30k floor, N=4 >= min_turns 4)
    for evt in make_turn(4, "Let us switch topics: what is vanadium flow battery capex?", "Capex is dominated by electrolyte and stack.", 40.0, prompt_tokens=35000):
        await session_service.append_event(session=session, event=evt)

    # Decision probe returns p = 0.88 >= 0.75 (pivot detected)
    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric", return_value=0.88), \
         patch("zocrates.agents.app.get_compaction_summarizer", return_value=summarizer):
        r4 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)

    assert r4 is True
    # 8 dialogue events + 1 compaction event = 9 events
    assert len(session.events) == 9
    comp_evt = session.events[-1]
    assert comp_evt.actions.compaction is not None
    assert comp_evt.actions.compaction.start_timestamp == 10.0
    assert comp_evt.actions.compaction.end_timestamp == 21.0

    # ADK prompt verification: Turns 1 & 2 replaced by summary; Turns 3 & 4 remain verbatim
    processed_turn4 = _process_compaction_events(session.events)
    p_texts = [p.content.parts[0].text for p in processed_turn4 if p.content and p.content.parts]
    assert p_texts[0] == "Summary of Turns 1 and 2"
    assert "Does temperature affect SEI growth?" in p_texts
    assert "Higher temperatures accelerate growth." in p_texts
    assert "Let us switch topics: what is vanadium flow battery capex?" in p_texts
    assert "Capex is dominated by electrolyte and stack." in p_texts

    # Turn 5: 38000 tokens. User asks followup on flow battery
    for evt in make_turn(5, "What are the key membrane degradation mechanisms in flow batteries?", "Crossover of vanadium species and chemical oxidation.", 50.0, prompt_tokens=38000):
        await session_service.append_event(session=session, event=evt)

    # Completed turns since last compaction: Turns 3, 4, and 5 (3 uncompacted turns)
    assert count_completed_turns_since_last_compaction(session) == 3
    # Since 3 < min_turns (4), envelope gate skips compaction
    r5 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)
    assert r5 is False
    # 9 previous events + 2 dialogue events = 11 events (no new compaction event appended)
    assert len(session.events) == 11

    # Context preservation check
    processed_turn5 = _process_compaction_events(session.events)
    p5_texts = [p.content.parts[0].text for p in processed_turn5 if p.content and p.content.parts]
    assert p5_texts[0] == "Summary of Turns 1 and 2"
    assert "What are the key membrane degradation mechanisms in flow batteries?" in p5_texts

    # Turn 6: 42000 tokens -> completed turns reaches 4 (Turns 3, 4, 5, 6)
    for evt in make_turn(6, "What strategies mitigate vanadium crossover?", "Nafion modification and size-exclusion membranes.", 60.0, prompt_tokens=42000):
        await session_service.append_event(session=session, event=evt)

    assert count_completed_turns_since_last_compaction(session) == 4

    rolling_summarizer = build_mock_summarizer("Consolidated summary covering Turns 1-4")
    with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric", return_value=0.90), \
         patch("zocrates.agents.app.get_compaction_summarizer", return_value=rolling_summarizer):
        r6 = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)

    assert r6 is True
    # 11 previous events + 2 dialogue events + 1 rolling compaction event = 14 events
    assert len(session.events) == 14
    comp2_evt = session.events[-1]
    assert comp2_evt.actions.compaction is not None
    # Preserves Turn 1 start (10.0) and extends to Turn 4 end (41.0)
    assert comp2_evt.actions.compaction.start_timestamp == 10.0
    assert comp2_evt.actions.compaction.end_timestamp == 41.0

    # ADK Subsumption: comp2 completely subsumes comp1, leaving single consolidated summary and Turns 5 & 6 verbatim
    processed_turn6 = _process_compaction_events(session.events)
    p6_texts = [p.content.parts[0].text for p in processed_turn6 if p.content and p.content.parts]
    assert p6_texts[0] == "Consolidated summary covering Turns 1-4"
    assert "What are the key membrane degradation mechanisms in flow batteries?" in p6_texts
    assert "What strategies mitigate vanadium crossover?" in p6_texts
