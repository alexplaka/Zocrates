"""Unit tests for Phase 3 Adaptive Context Compaction Integration.

Validates:
- Retention Slicing ($K=2$): Candidate slice covers Turns 1 .. N-K; Turns N-1 and N
  have timestamps > end_timestamp and remain 100% verbatim in ADK's coordinator prompt.
- Short sessions: Returns None when completed turns <= retention_turns ($N \\le K$).
- Rolling Consolidation: Second compaction integrates previous summary + new completed turns,
  spans [comp1.start_timestamp, comp2.end_timestamp], and subsumes earlier compaction in ADK.
- Scope Isolation: Pipeline subagent events (isolation_scope is not None) are strictly excluded.
- Rewind Mechanics: Rewound invocations are filtered before slicing and omitted from summaries.
- HITL Immunity: Mid-turn tool calls and confirmations never trigger compaction.
- Concurrency & Fail-Open: StaleSessionError is caught defensively without session corruption.
- OpenTelemetry Observability: Emits `compact_session_history` span with LLM kind and domain metadata.
- Runner Post-Invocation Hook: `patched_post_invocation_compaction` runs adaptive compaction and appends event.
"""

from __future__ import annotations

import asyncio
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

from zocrates.agents.common.compaction import (
    FailOpenLlmEventSummarizer,
    ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    compact_session_adaptive,
    run_adaptive_post_invocation_compaction,
)
from zocrates.agents.common.adk_compat import patch_compaction_subagent_isolation


# ---------------------------------------------------------------------------
# Test Helpers & Mock LLM
# ---------------------------------------------------------------------------

class MockSummarizerLlm(BaseLlm):
    """Mock LLM for compaction tests."""

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


def make_turn(turn_idx: int, q: str, a: str, start_ts: float) -> list[Event]:
    """Helper creating a 2-event completed turn."""
    inv_id = f"inv_{turn_idx}"
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
        ),
    ]


def build_mock_summarizer(summary_text: str = "Mock academic compaction summary.") -> FailOpenLlmEventSummarizer:
    """Create a FailOpenLlmEventSummarizer wrapped around MockSummarizerLlm."""
    return FailOpenLlmEventSummarizer(
        llm=MockSummarizerLlm(summary_text=summary_text),
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_turns_preserves_latest_k_turns_verbatim():
    """Verify that with N=4 completed turns and K=2, candidate slice covers Turns 1 and 2,
    while Turns 3 and 4 have timestamps > end_timestamp and remain 100% verbatim in ADK prompt.
    """
    events = []
    # Turn 1: ts=10.0, 11.0
    events.extend(make_turn(1, "Question 1", "Answer 1", 10.0))
    # Turn 2: ts=20.0, 21.0
    events.extend(make_turn(2, "Question 2", "Answer 2", 20.0))
    # Turn 3: ts=30.0, 31.0
    events.extend(make_turn(3, "Question 3", "Answer 3", 30.0))
    # Turn 4: ts=40.0, 41.0
    events.extend(make_turn(4, "Question 4", "Answer 4", 40.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Summary of Turns 1 and 2")
    cfg = {"adaptive_compaction_retention_turns": 2}

    compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)

    assert compaction_event is not None
    comp = compaction_event.actions.compaction
    assert comp is not None
    # Start and end timestamps span exactly Turn 1 and Turn 2
    assert comp.start_timestamp == 10.0
    assert comp.end_timestamp == 21.0

    # Verify prompt received only Turns 1 and 2
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    assert "Question 1" in prompt
    assert "Answer 1" in prompt
    assert "Question 2" in prompt
    assert "Answer 2" in prompt
    assert "Question 3" not in prompt
    assert "Question 4" not in prompt

    # Verify ADK prompt reconstruction with _process_compaction_events:
    # Turns 3 and 4 have timestamps > 21.0 and MUST remain 100% verbatim
    all_events = list(events)
    all_events.append(compaction_event)
    processed = _process_compaction_events(all_events)

    processed_texts = [
        p.content.parts[0].text for p in processed if p.content and p.content.parts
    ]
    # Compacted model summary replaces Turns 1 and 2
    assert processed_texts[0] == "Summary of Turns 1 and 2"
    # Turns 3 and 4 are preserved verbatim
    assert "Question 3" in processed_texts
    assert "Answer 3" in processed_texts
    assert "Question 4" in processed_texts
    assert "Answer 4" in processed_texts


@pytest.mark.asyncio
async def test_short_sessions_below_retention_returns_none():
    """Verify that when completed turns <= retention_turns (e.g. N=2, K=2), returns None."""
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    events.extend(make_turn(2, "Q2", "A2", 20.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer()
    cfg = {"adaptive_compaction_retention_turns": 2}

    event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert event is None
    assert summarizer._llm.calls == 0


@pytest.mark.asyncio
async def test_rolling_consolidation_subsumes_earlier_compaction():
    """Verify that a second compaction consolidates prior summary + new turns,
    spans [comp1.start_timestamp, comp2.end_timestamp], and subsumes earlier event in ADK.
    """
    events = []
    # Turn 1: ts=10.0, 11.0
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    # Turn 2: ts=20.0, 21.0
    events.extend(make_turn(2, "Q2", "A2", 20.0))

    # First compaction event spanning [10.0, 21.0]
    comp1 = Event(
        author="user",
        invocation_id="inv_c1",
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

    # Turn 3: ts=30.0, 31.0
    events.extend(make_turn(3, "Q3", "A3", 30.0))
    # Turn 4: ts=40.0, 41.0
    events.extend(make_turn(4, "Q4", "A4", 40.0))
    # Turn 5: ts=50.0, 51.0
    events.extend(make_turn(5, "Q5", "A5", 50.0))
    # Turn 6: ts=60.0, 61.0
    events.extend(make_turn(6, "Q6", "A6", 60.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Consolidated summary covering Turns 1-4")
    cfg = {"adaptive_compaction_retention_turns": 2}

    # N=6 completed turns, K=2 retention -> candidate slice up to Turn 4 (N-K=4)
    # New completed turns between comp1.end_timestamp (21.0) and Turn 4 (41.0) are Turns 3 and 4
    comp2 = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)

    assert comp2 is not None
    c2 = comp2.actions.compaction
    assert c2 is not None
    # Preserves comp1.start_timestamp anchor (10.0)
    assert c2.start_timestamp == 10.0
    # Extends to Turn 4's final timestamp (41.0)
    assert c2.end_timestamp == 41.0

    # Verify rolling prompt formatting: includes prior summary AND new completed turns
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    assert "### Existing Conversation Summary" in prompt
    assert "First summary covering Turns 1-2" in prompt
    assert "### New Completed Turns to Integrate" in prompt
    assert "Q3" in prompt
    assert "A3" in prompt
    assert "Q4" in prompt
    assert "A4" in prompt
    # Retained turns (Turns 5 and 6) are excluded from the prompt
    assert "Q5" not in prompt
    assert "Q6" not in prompt

    # Verify ADK subsumption: comp2 completely subsumes comp1
    all_events = list(events)
    all_events.append(comp2)
    processed = _process_compaction_events(all_events)

    processed_texts = [
        p.content.parts[0].text for p in processed if p.content and p.content.parts
    ]
    # comp1 is subsumed, comp2 is the single consolidated model summary
    assert processed_texts[0] == "Consolidated summary covering Turns 1-4"
    # Turns 5 and 6 remain verbatim
    assert "Q5" in processed_texts
    assert "A5" in processed_texts
    assert "Q6" in processed_texts
    assert "A6" in processed_texts


@pytest.mark.asyncio
async def test_rolling_consolidation_returns_none_if_no_new_turns():
    """Verify that if all candidate turns (Turns 1 .. N-K) were already subsumed by previous
    compaction, compact_session_adaptive returns None.
    """
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    events.extend(make_turn(2, "Q2", "A2", 20.0))

    comp1 = Event(
        author="user",
        invocation_id="inv_c1",
        timestamp=22.0,
        actions=EventActions(
            compaction=EventCompaction(
                start_timestamp=10.0,
                end_timestamp=21.0,
                compacted_content=Content(role="model", parts=[Part.from_text(text="Prior summary")]),
            )
        ),
    )
    events.append(comp1)

    # Turns 3 and 4 are added. N=4, K=2 -> candidate slice covers Turns 1 and 2
    # But Turns 1 and 2 have ts <= 21.0 (already subsumed) -> no new turns to integrate!
    events.extend(make_turn(3, "Q3", "A3", 30.0))
    events.extend(make_turn(4, "Q4", "A4", 40.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer()
    cfg = {"adaptive_compaction_retention_turns": 2}

    event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert event is None
    assert summarizer._llm.calls == 0


@pytest.mark.asyncio
async def test_slicing_applies_rewinds_correctly():
    """Verify that rewound invocations are filtered out before slicing and omitted from summaries."""
    events = []
    # Turn 1
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    # Turn 2
    events.extend(make_turn(2, "Q2", "A2", 20.0))
    # Turn 3 (to be rewound)
    events.extend(make_turn(3, "Q3 BAD", "A3 BAD", 30.0))
    # Turn 4 (to be rewound)
    events.extend(make_turn(4, "Q4 BAD", "A4 BAD", 40.0))

    # User rewound before inv_3
    rewind_event = Event(
        author="user",
        invocation_id="inv_rewind",
        timestamp=45.0,
        actions=EventActions(rewind_before_invocation_id="inv_3"),
    )
    events.append(rewind_event)

    # Turn 5 (new Turn 3 in live history)
    events.extend(make_turn(5, "Q5 GOOD", "A5 GOOD", 50.0))
    # Turn 6 (new Turn 4 in live history)
    events.extend(make_turn(6, "Q6 GOOD", "A6 GOOD", 60.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer("Summary after rewind")
    cfg = {"adaptive_compaction_retention_turns": 2}

    # Live completed turns: Turn 1, Turn 2, Turn 5, Turn 6 (N=4)
    # Retained turns (K=2): Turn 5 and Turn 6
    # Summarized turns: Turn 1 and Turn 2
    compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)

    assert compaction_event is not None
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    # Bad rewound turns MUST NOT be in the prompt
    assert "BAD" not in prompt
    assert "Q3" not in prompt
    assert "Q4" not in prompt
    # Valid candidate turns ARE in the prompt
    assert "Q1" in prompt
    assert "Q2" in prompt


@pytest.mark.asyncio
async def test_subagent_pipeline_events_strictly_excluded():
    """Verify that subagent retrieval pipeline events (isolation_scope is not None)
    are excluded from the compaction slice.
    """
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))

    # Internal subagent pipeline events during Turn 2
    events.append(
        Event(
            author="user",
            invocation_id="inv_2",
            timestamp=20.0,
            content=Content(parts=[Part.from_text(text="Q2 with search")]),
        )
    )
    events.append(
        Event(
            author="library_search_agent",
            invocation_id="inv_2",
            isolation_scope="search_pipeline:fc123",
            timestamp=20.2,
            content=Content(parts=[Part.from_text(text="INTERNAL_PIPELINE_CHUNK_DATA")]),
        )
    )
    events.append(
        Event(
            author="coordinator_agent",
            invocation_id="inv_2",
            timestamp=21.0,
            content=Content(parts=[Part.from_text(text="A2 search synthesis")]),
        )
    )
    events.extend(make_turn(3, "Q3", "A3", 30.0))
    events.extend(make_turn(4, "Q4", "A4", 40.0))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer()
    cfg = {"adaptive_compaction_retention_turns": 2}

    event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)
    assert event is not None
    prompt = summarizer._llm.last_prompt
    assert prompt is not None
    assert "INTERNAL_PIPELINE_CHUNK_DATA" not in prompt


@pytest.mark.asyncio
async def test_hitl_interruption_immunity():
    """Verify that turns interrupted mid-turn for HITL (open function_call or confirmations)
    never trigger adaptive compaction.
    """
    # 1. Open function call (search_budget_checkpoint)
    events_open_call = [
        Event(author="user", timestamp=10.0, invocation_id="inv_1", content=Content(parts=[Part.from_text(text="search")])),
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
    result = await run_adaptive_post_invocation_compaction(runner, session_hitl, cfg={"enable_adaptive_compaction": True})
    assert result is False

    # 2. Awaiting tool confirmations
    events_conf = [
        Event(author="user", timestamp=10.0, invocation_id="inv_1", content=Content(parts=[Part.from_text(text="delete doc")])),
        Event(
            author="coordinator_agent",
            timestamp=20.0,
            invocation_id="inv_1",
            actions=EventActions(requested_tool_confirmations={"c1": ToolConfirmation(hint="confirm")}),
        ),
    ]
    session_conf = Session(id="s_conf", app_name="zocrates", user_id="u1")
    session_conf.events = events_conf

    result_conf = await run_adaptive_post_invocation_compaction(runner, session_conf, cfg={"enable_adaptive_compaction": True})
    assert result_conf is False


@pytest.mark.asyncio
async def test_stale_session_concurrency_fail_open():
    """Verify that concurrent modification (e.g. StaleSessionError or DB error) during append_event
    is caught defensively and returns False without session corruption.
    """
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10)))

    session = Session(id="s_stale", app_name="zocrates", user_id="u1")
    session.events = events

    # Mock session service where append_event raises StaleSessionError / Exception
    mock_service = MagicMock()
    mock_service.append_event = AsyncMock(side_effect=Exception("StaleSessionError: revision marker conflict"))

    runner = Runner(app=MagicMock(), session_service=mock_service)

    cfg = {
        "enable_adaptive_compaction": True,
        "adaptive_compaction_retention_turns": 2,
        "adaptive_compaction_min_turns": 4,
        "adaptive_compaction_token_floor": 0,
    }

    # Patch should_trigger_adaptive_compaction to return True
    with patch("zocrates.agents.common.compaction.should_trigger_adaptive_compaction", return_value=(True, "forced_compress")), \
         patch("zocrates.agents.common.compaction.compact_session_adaptive", return_value=Event(author="user", invocation_id="c_evt")):
        result = await run_adaptive_post_invocation_compaction(runner, session, cfg=cfg)

    # Caught defensively, returns False
    assert result is False


@pytest.mark.asyncio
async def test_telemetry_spans_emitted_for_summarizer():
    """Verify that compact_session_adaptive emits `compact_session_history` span
    with LLM kind and full domain metadata attributes.
    """
    events = []
    events.extend(make_turn(1, "Q1", "A1", 10.0))
    events.extend(make_turn(2, "Q2", "A2", 20.0))
    events.extend(make_turn(3, "Q3", "A3", 30.0))
    events.extend(make_turn(4, "Q4", "A4", 40.0))

    session = Session(id="s_telemetry", app_name="zocrates", user_id="u1")
    session.events = events

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    summarizer = build_mock_summarizer("Telemetry test summary")
    cfg = {"adaptive_compaction_retention_turns": 2}

    with patch.object(trace, "get_tracer", side_effect=provider.get_tracer):
        compaction_event = await compact_session_adaptive(session, cfg=cfg, summarizer=summarizer)

    assert compaction_event is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "compact_session_history"
    assert span.attributes["openinference.span.kind"] == "LLM"
    assert span.attributes["llm.model_name"] == "mock-compactor-model"
    assert span.attributes["gen_ai.request.model"] == "mock-compactor-model"
    assert "temperature" in span.attributes["llm.invocation_parameters"]
    assert span.attributes["input.mime_type"] == "text/plain"
    assert span.attributes["output.mime_type"] == "text/plain"
    assert span.attributes["output.value"] == "Telemetry test summary"

    # Token counts
    assert span.attributes["llm.token_count.prompt"] == 150
    assert span.attributes["llm.token_count.completion"] == 50
    assert span.attributes["llm.token_count.total"] == 200

    # Domain attributes
    assert span.attributes["compaction.summarize.start_timestamp"] == 10.0
    assert span.attributes["compaction.summarize.end_timestamp"] == 21.0
    assert span.attributes["compaction.summarize.slice_turns"] == 2
    assert span.attributes["compaction.summarize.retained_turns"] == 2
    assert span.attributes["compaction.summarize.latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_compact_session_adaptive_fails_open_on_llm_error():
    """Verify that compact_session_adaptive fails open cleanly to None when LLM raises."""
    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10)))

    session = Session(id="s1", app_name="zocrates", user_id="u1")
    session.events = events

    summarizer = build_mock_summarizer()
    summarizer._llm.should_raise = RuntimeError("LLM API Timeout or Network error")

    event = await compact_session_adaptive(session, cfg={}, summarizer=summarizer)
    assert event is None


@pytest.mark.asyncio
async def test_hook_integration_patched_post_invocation_compaction():
    """Verify that Runner._run_post_invocation_compaction invokes run_adaptive_post_invocation_compaction
    and appends the compaction event to the session.
    """
    patch_compaction_subagent_isolation()

    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10)))

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="zocrates", user_id="u1", session_id="s_hook"
    )
    for e in events:
        await session_service.append_event(session=session, event=e)

    mock_app = MagicMock()
    mock_app.events_compaction_config = None
    runner = Runner(app=mock_app, session_service=session_service)

    mock_comp_event = Event(
        author="user",
        invocation_id="inv_hook_comp",
        timestamp=45.0,
        actions=EventActions(
            compaction=EventCompaction(
                start_timestamp=10.0,
                end_timestamp=21.0,
                compacted_content=Content(role="model", parts=[Part.from_text(text="Hook test summary")]),
            )
        ),
    )

    with patch("zocrates.agents.common.compaction.load_zocrates_config", return_value={"enable_adaptive_compaction": True}), \
         patch("zocrates.agents.common.compaction.should_trigger_adaptive_compaction", return_value=(True, "test_compress")), \
         patch("zocrates.agents.common.compaction.compact_session_adaptive", return_value=mock_comp_event):
        await runner._run_post_invocation_compaction(session=session, skip_token_compaction=True)

    # Verify compaction event was appended to session.events
    assert len(session.events) == len(events) + 1
    assert session.events[-1].invocation_id == "inv_hook_comp"
    assert session.events[-1].actions.compaction.compacted_content.parts[0].text == "Hook test summary"


@pytest.mark.asyncio
async def test_compact_session_adaptive_litellm_pydantic_resilience():
    """Verify that compact_session_adaptive with a real LiteLlm instance (Pydantic BaseModel)
    does not fail on extra_body field assignment and succeeds without ValueError.
    """
    from zocrates.agents.common.compaction import build_compaction_llm, FailOpenLlmEventSummarizer, ZOCRATES_COMPACTION_PROMPT_TEMPLATE
    from zocrates.config import load_zocrates_config

    events = []
    for i in range(1, 5):
        events.extend(make_turn(i, f"Q{i}", f"A{i}", float(i * 10)))

    session = Session(id="s_litellm_test", app_name="zocrates", user_id="u1")
    session.events = events

    real_litellm = build_compaction_llm(load_zocrates_config())

    async def mock_generate(req: Any, stream: bool = False):
        usage = GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=50, total_token_count=150
        )
        content = Content(role="model", parts=[Part.from_text(text="Resilience summary")])
        yield LlmResponse(content=content, usage_metadata=usage)

    object.__setattr__(real_litellm, "generate_content_async", mock_generate)

    summarizer = FailOpenLlmEventSummarizer(
        llm=real_litellm,
        prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    )

    compaction_event = await compact_session_adaptive(session, cfg={"adaptive_compaction_retention_turns": 2}, summarizer=summarizer)
    assert compaction_event is not None
    assert compaction_event.actions.compaction is not None
    assert compaction_event.actions.compaction.compacted_content.parts[0].text == "Resilience summary"
