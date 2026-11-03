"""Unit tests for Phase 2 Post-Turn Decision Engine Core (Adaptive Compaction).

Validates:
- get_coordinator_prompt_tokens (scope filtering, latest unscoped event extraction)
- Turn grouping, incomplete turn trimming, and invariant handling (effective_min_turns = max(min_turns, k + 1))
- State extraction & formatting (conversational turns, research turns with conclude_output, clean markdown, partitioning)
- evaluate_adaptive_compaction_rubric (transport, retry on 429, OpenTelemetry span, fail-open resilience)
- should_trigger_adaptive_compaction (toggle, envelope gate, hard token ceiling, prob thresholds, fail-open)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from google.adk.events import Event
from google.adk.events.event_actions import EventActions, EventCompaction
from google.genai.types import Content, FunctionCall, FunctionResponse, Part

from zocrates.agents.common.adaptive_decision import (
    DECISION_PROMPT_INSTRUCTIONS,
    CompletedTurn,
    count_completed_turns_since_last_compaction,
    evaluate_adaptive_compaction_rubric,
    extract_decision_state,
    get_completed_turns,
    get_coordinator_prompt_tokens,
    is_local_model,
    probe_local_laya_health,
    should_trigger_adaptive_compaction,
)
from zocrates.agents.common.state_keys import RESEARCH_TURN_HISTORY


# ---------------------------------------------------------------------------
# Helpers for Constructing Test Events and Sessions
# ---------------------------------------------------------------------------

def make_user_event(
    text: str,
    invocation_id: str = "inv_1",
    timestamp: float = 1.0,
) -> Event:
    """Create a user dialogue event."""
    return Event(
        author="user",
        invocation_id=invocation_id,
        timestamp=timestamp,
        content=Content(parts=[Part.from_text(text=text)]),
    )


def make_coordinator_event(
    text: str,
    invocation_id: str = "inv_1",
    timestamp: float = 2.0,
    prompt_tokens: int | None = None,
    thought: str | None = None,
    function_call: FunctionCall | None = None,
    function_response: FunctionResponse | None = None,
) -> Event:
    """Create a coordinator agent dialogue event with optional parts and usage metadata."""
    parts = []
    if thought:
        parts.append(Part(text=thought, thought=True))
    if function_call:
        parts.append(Part(function_call=function_call))
    if function_response:
        parts.append(Part(function_response=function_response))
    if text:
        parts.append(Part.from_text(text=text))

    usage = None
    if prompt_tokens is not None:
        usage = {"prompt_token_count": prompt_tokens}

    return Event(
        author="coordinator_agent",
        invocation_id=invocation_id,
        timestamp=timestamp,
        content=Content(parts=parts),
        usage_metadata=usage,
    )


def make_scoped_subagent_event(
    text: str,
    invocation_id: str = "inv_1",
    timestamp: float = 1.5,
    scope: str = "search_pipeline:fc1",
    prompt_tokens: int | None = None,
) -> Event:
    """Create a scoped subagent retrieval pipeline event."""
    usage = None
    if prompt_tokens is not None:
        usage = {"prompt_token_count": prompt_tokens}

    return Event(
        author="library_search_agent",
        invocation_id=invocation_id,
        isolation_scope=scope,
        timestamp=timestamp,
        content=Content(parts=[Part.from_text(text=text)]),
        usage_metadata=usage,
    )


class MockSession:
    """Mock ADK Session for testing."""

    def __init__(
        self,
        events: list[Event] | None = None,
        state: dict[str, Any] | None = None,
        session_id: str = "test-session-123",
    ):
        self.events = events or []
        self.state = state or {}
        self.id = session_id
        self.session_id = session_id


# ---------------------------------------------------------------------------
# Test Suite 1: get_coordinator_prompt_tokens
# ---------------------------------------------------------------------------

class TestCoordinatorPromptTokens:
    def test_extracts_tokens_from_latest_unscoped_event(self):
        """Extracts prompt token count strictly from latest unscoped coordinator event."""
        e1 = make_coordinator_event(text="A1", prompt_tokens=10000, timestamp=1.0)
        # Subagent scoped event with high prompt token count
        e_sub = make_scoped_subagent_event(text="sub", prompt_tokens=90000, timestamp=2.0)
        # Latest unscoped event
        e2 = make_coordinator_event(text="A2", prompt_tokens=32000, timestamp=3.0)

        tokens = get_coordinator_prompt_tokens([e1, e_sub, e2])
        assert tokens == 32000

    def test_ignores_scoped_events_at_tail(self):
        """If latest event is scoped, ignores it and finds latest unscoped coordinator event."""
        e1 = make_coordinator_event(text="A1", prompt_tokens=25000, timestamp=1.0)
        e_sub = make_scoped_subagent_event(text="sub", prompt_tokens=85000, timestamp=2.0)

        tokens = get_coordinator_prompt_tokens([e1, e_sub])
        assert tokens == 25000

    def test_handles_dict_usage_metadata(self):
        """Extracts prompt tokens when usage_metadata is a dict rather than an object."""
        e = Event(
            author="coordinator_agent",
            timestamp=1.0,
            content=Content(parts=[Part.from_text(text="A1")]),
            usage_metadata={"prompt_token_count": 42000},
        )
        assert get_coordinator_prompt_tokens([e]) == 42000

    def test_returns_none_when_no_unscoped_usage_metadata(self):
        """Returns None if all unscoped events lack usage_metadata."""
        e1 = make_coordinator_event(text="A1", prompt_tokens=None, timestamp=1.0)
        e_sub = make_scoped_subagent_event(text="sub", prompt_tokens=50000, timestamp=2.0)
        assert get_coordinator_prompt_tokens([e1, e_sub]) is None

    def test_returns_none_for_empty_list(self):
        """Returns None when event list is empty."""
        assert get_coordinator_prompt_tokens([]) is None


# ---------------------------------------------------------------------------
# Test Suite 2: Turn Grouping, Retention Invariant & Slicing
# ---------------------------------------------------------------------------

class TestTurnGroupingAndInvariants:
    def test_effective_min_turns_clamping_and_warning(self, caplog):
        """Asserts raw_min_turns <= k automatically clamps effective_min_turns = k + 1 with warning."""
        cfg = {
            "adaptive_compaction_min_turns": 1,
            "adaptive_compaction_retention_turns": 2,
            "adaptive_compaction_token_floor": 0,
        }
        # 2 turns in session
        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("A1", invocation_id="i1", timestamp=2.0)
        e3 = make_user_event("Q2", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event("A2", invocation_id="i2", timestamp=4.0)
        session = MockSession(events=[e1, e2, e3, e4])

        with caplog.at_level("WARNING"):
            should_comp, reason = should_trigger_adaptive_compaction(session, cfg)

        assert not should_comp
        assert "turn_count_below_min (2 < 3)" in reason
        assert "must be strictly greater than retention_turns (2)" in caplog.text

    def test_incomplete_turns_dropped(self):
        """Trailing unanswered user query is dropped from completed turns."""
        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("A1", invocation_id="i1", timestamp=2.0)
        e3 = make_user_event("Q2 pending", invocation_id="i2", timestamp=3.0)
        # No assistant event for i2

        session = MockSession(events=[e1, e2, e3])
        turns = get_completed_turns(session)
        assert len(turns) == 1
        assert turns[0].user_query == "Q1"
        assert turns[0].assistant_response == "A1"

    def test_rewinds_applied_to_completed_turns(self):
        """Rewound invocations are filtered out of completed turns."""
        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("A1", invocation_id="i1", timestamp=2.0)
        e3 = make_user_event("Q2 rewound", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event("A2 rewound", invocation_id="i2", timestamp=4.0)
        e_rewind = Event(
            author="user",
            invocation_id="i3",
            timestamp=5.0,
            actions=EventActions(rewind_before_invocation_id="i2"),
        )
        session = MockSession(events=[e1, e2, e3, e4, e_rewind])
        turns = get_completed_turns(session)
        assert len(turns) == 1
        assert turns[0].user_query == "Q1"

    def test_completed_turns_since_last_compaction(self):
        """Turns before latest compaction end_timestamp are excluded when since_last_compaction=True."""
        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("A1", invocation_id="i1", timestamp=2.0)
        comp_event = Event(
            author="user",
            invocation_id="i_comp",
            timestamp=3.0,
            actions=EventActions(
                compaction=EventCompaction(
                    start_timestamp=1.0,
                    end_timestamp=2.0,
                    compacted_content=Content(parts=[Part.from_text(text="Summary")]),
                )
            ),
        )
        e3 = make_user_event("Q2 new", invocation_id="i2", timestamp=4.0)
        e4 = make_coordinator_event("A2 new", invocation_id="i2", timestamp=5.0)

        session = MockSession(events=[e1, e2, comp_event, e3, e4])
        turns_all = get_completed_turns(session, since_last_compaction=False)
        assert len(turns_all) == 2

        turns_since = get_completed_turns(session, since_last_compaction=True)
        assert len(turns_since) == 1
        assert turns_since[0].user_query == "Q2 new"
        assert count_completed_turns_since_last_compaction(session) == 1


# ---------------------------------------------------------------------------
# Test Suite 3: State Formatting and Extraction
# ---------------------------------------------------------------------------

class TestStateFormattingAndExtraction:
    def test_conversational_turns_untruncated_and_thought_stripped(self):
        """Conversational turns preserve untruncated text, strip thoughts, and partition into serial Markdown."""
        e1 = make_user_event("What is photosynthesis?", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event(
            text="Photosynthesis converts solar energy into biochemical energy.",
            invocation_id="i1",
            timestamp=2.0,
            thought="Thinking deeply about chlorophyll...",
        )
        e3 = make_user_event("What are the main pigments?", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event(
            text="Chlorophyll a and b are primary, while carotenoids act as accessory pigments.",
            invocation_id="i2",
            timestamp=4.0,
        )
        e5 = make_user_event("Now switching gears, what are perovskite solar cells?", invocation_id="i3", timestamp=5.0)
        e6 = make_coordinator_event(
            text="Perovskite cells utilize organic-inorganic lead halide absorber layers.",
            invocation_id="i3",
            timestamp=6.0,
        )

        session = MockSession(events=[e1, e2, e3, e4, e5, e6])
        state = extract_decision_state(session, k=2)

        # Thought stripped
        assert "Thinking deeply" not in state["earlier_discussion"]
        assert "Thinking deeply" not in state["latest_turn"]

        # Partitioning check
        assert "**User**: What is photosynthesis?" in state["earlier_discussion"]
        assert "**Assistant**: Photosynthesis converts solar energy into biochemical energy." in state["earlier_discussion"]
        assert "**User**: What are the main pigments?" in state["earlier_discussion"]
        assert "**Assistant**: Chlorophyll a and b" in state["earlier_discussion"]

        # Turn 3 is strictly latest_turn
        assert "**User**: Now switching gears, what are perovskite solar cells?" in state["latest_turn"]
        assert "**Assistant**: Perovskite cells utilize organic-inorganic" in state["latest_turn"]
        assert "photosynthesis" not in state["latest_turn"].lower()

        # Format hygiene: no ### Turn headers, no bullet list prefixes
        assert "###" not in state["earlier_discussion"]
        assert "###" not in state["latest_turn"]

    def test_research_turns_extract_conclude_output_and_bypass_raw_synthesis(self):
        """Research pipeline turns extract concise summary from conclude_output, bypassing full synthesis & raw chunks."""
        # Invocation 1: Research pipeline turn
        e1 = make_user_event("What causes lithium plating in graphite anodes?", invocation_id="i1", timestamp=1.0)
        huge_synth = (
            "Here is the detailed synthesis with 2000 words. According to [1] and [2], "
            "plating occurs at low temperatures [Chunk 42]. Furthermore [E 3] shows SEI growth.\n\n"
            "# Cited Sources\n"
            "- [1] Smith et al. (2020)\n"
            "- [2] Jones et al. (2021)"
        )
        e2 = make_coordinator_event(
            text=huge_synth,
            invocation_id="i1",
            timestamp=2.0,
            function_call=FunctionCall(name="search_library", id="fc1", args={"query": "lithium plating"}),
        )

        # Invocation 2: Conversational follow-up
        e3 = make_user_event("How about flow batteries?", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event(
            text="Vanadium flow batteries decouple power and energy capacity.",
            invocation_id="i2",
            timestamp=4.0,
        )

        state_dict = {
            RESEARCH_TURN_HISTORY: [
                {
                    "invocation_id": "i1",
                    "pipeline": "library_search",
                    "user_query": "What causes lithium plating in graphite anodes?",
                    "search_goal": "lithium plating causes",
                    "conclude_output": {
                        "summary": "Fast charging at low temperatures drives graphite potential below 0 V, precipitating metallic lithium plating.",
                        "gaps": [],
                        "notes": None,
                    },
                }
            ]
        }

        session = MockSession(events=[e1, e2, e3, e4], state=state_dict)
        state = extract_decision_state(session, k=2)

        # Invocation 1 should use clean conclude_output summary (<150 words)
        earlier = state["earlier_discussion"]
        assert "Fast charging at low temperatures drives graphite potential below 0 V, precipitating metallic lithium plating." in earlier
        assert "2000 words" not in earlier
        assert "[Chunk 42]" not in earlier
        assert "# Cited Sources" not in earlier
        assert "[1, 2]" not in earlier

    def test_research_summary_word_ceiling(self):
        """Long conclude_output summaries exceeding 150 words are clamped."""
        long_summary = " ".join([f"word{i}" for i in range(200)])
        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("Full synthesis", invocation_id="i1", timestamp=2.0)
        e3 = make_user_event("Q2", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event("A2", invocation_id="i2", timestamp=4.0)

        session = MockSession(
            events=[e1, e2, e3, e4],
            state={
                RESEARCH_TURN_HISTORY: [
                    {
                        "invocation_id": "i1",
                        "user_query": "Q1",
                        "conclude_output": {"summary": long_summary},
                    }
                ]
            },
        )
        state = extract_decision_state(session, k=2)
        earlier_asst = state["earlier_discussion"].split("**Assistant**:")[-1].strip()
        words = earlier_asst.split()
        assert len(words) <= 150
        assert earlier_asst.endswith("...")

    def test_zero_tool_call_leakage(self):
        """Intermediate function calls and arguments never leak into extracted state."""
        e1 = make_user_event("Search my library for Cas9", invocation_id="i1", timestamp=1.0)
        fc = FunctionCall(name="search_library", id="fc1", args={"query": "Cas9 mechanism", "limit": 10})
        fr = FunctionResponse(name="search_library", id="fc1", response={"result": "raw tool output"})
        e2 = make_coordinator_event(
            text="CRISPR-Cas9 acts as RNA-guided molecular scissors.",
            invocation_id="i1",
            timestamp=2.0,
            function_call=fc,
            function_response=fr,
        )
        e3 = make_user_event("Next question", invocation_id="i2", timestamp=3.0)
        e4 = make_coordinator_event("Next answer", invocation_id="i2", timestamp=4.0)

        session = MockSession(events=[e1, e2, e3, e4])
        state = extract_decision_state(session, k=2)

        assert "fc1" not in state["earlier_discussion"]
        assert "search_library" not in state["earlier_discussion"]
        assert "raw tool output" not in state["earlier_discussion"]

    def test_insufficient_dialogue_state_returns_empty_earlier_discussion(self):
        """Sessions with fewer than 2 completed turns return empty earlier_discussion."""
        session0 = MockSession(events=[])
        assert extract_decision_state(session0) == {"earlier_discussion": "", "latest_turn": ""}

        e1 = make_user_event("Q1", invocation_id="i1", timestamp=1.0)
        e2 = make_coordinator_event("A1", invocation_id="i1", timestamp=2.0)
        session1 = MockSession(events=[e1, e2])
        state1 = extract_decision_state(session1)
        assert state1["earlier_discussion"] == ""
        assert "**User**: Q1" in state1["latest_turn"]


# ---------------------------------------------------------------------------
# Test Suite 4: evaluate_adaptive_compaction_rubric
# ---------------------------------------------------------------------------

class TestEvaluateAdaptiveCompactionRubric:
    @patch("requests.post")
    def test_successful_probe_parsing(self, mock_post):
        """Parses valid probability from OpenRouter decisions API and sets span attributes."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "answers": {
                "compaction_warranted": {
                    "noul": 0.845
                }
            },
            "usage": {"input_tokens": 150, "output_tokens": 1},
        }
        mock_post.return_value = mock_resp

        state = {
            "earlier_discussion": "**User**: Q1\n**Assistant**: A1",
            "latest_turn": "**User**: Q2\n**Assistant**: A2",
        }
        cfg = {
            "adaptive_compaction_decision_model": "inception/mercury-decide:free",
            "adaptive_compaction_threshold": 0.75,
        }

        prob = evaluate_adaptive_compaction_rubric(state, cfg, api_key="sk-test-key")
        assert prob == 0.845

        # Verify payload sent to OpenRouter
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        sent_json = kwargs["json"]
        assert sent_json["model"] == "inception/mercury-decide:free"
        assert sent_json["state"]["earlier_discussion"] == state["earlier_discussion"]
        assert sent_json["state"]["latest_turn"] == state["latest_turn"]
        assert sent_json["questions"]["compaction_warranted"]["instructions"] == DECISION_PROMPT_INSTRUCTIONS

    @patch("requests.post")
    @patch("time.sleep")
    def test_http_429_retry(self, mock_sleep, mock_post):
        """Retries after 250ms when receiving HTTP 429 rate limit."""
        resp_429 = MagicMock(status_code=429, text="Rate limit")
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "answers": {"compaction_warranted": {"noul": 0.78}}
        }
        mock_post.side_effect = [resp_429, resp_200]

        state = {"earlier_discussion": "d1", "latest_turn": "d2"}
        cfg = {"adaptive_compaction_threshold": 0.75}

        prob = evaluate_adaptive_compaction_rubric(state, cfg, api_key="sk-test-key")
        assert prob == 0.78
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(0.250)

    @patch("requests.post")
    def test_fail_open_on_timeout(self, mock_post):
        """Fails open to None on request timeout."""
        mock_post.side_effect = requests.Timeout("Connection timed out")
        state = {"earlier_discussion": "d1", "latest_turn": "d2"}
        cfg = {"adaptive_compaction_timeout_sec": 5.0}

        prob = evaluate_adaptive_compaction_rubric(state, cfg, api_key="sk-test-key")
        assert prob is None

    @patch("requests.post")
    def test_fail_open_on_http_500(self, mock_post):
        """Fails open to None on upstream HTTP 500 error."""
        resp_500 = MagicMock(status_code=500, text="Internal Server Error")
        mock_post.return_value = resp_500

        prob = evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, {}, api_key="k")
        assert prob is None

    @patch("requests.post")
    def test_fail_open_on_malformed_json_or_nan(self, mock_post):
        """Fails open to None when response is missing noul or returns NaN."""
        # Missing noul
        mock_resp1 = MagicMock(status_code=200)
        mock_resp1.json.return_value = {"answers": {"compaction_warranted": {}}}
        mock_post.return_value = mock_resp1
        assert evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, {}, api_key="k") is None

        # NaN
        mock_resp2 = MagicMock(status_code=200)
        mock_resp2.json.return_value = {"answers": {"compaction_warranted": {"noul": float("nan")}}}
        mock_post.return_value = mock_resp2
        assert evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, {}, api_key="k") is None

    def test_fail_open_when_missing_api_key(self):
        """Fails open to None when no API key is configured for remote probe."""
        with patch("zocrates.agents.common.adaptive_decision.get_secure_api_key", return_value=""):
            prob = evaluate_adaptive_compaction_rubric(
                {"earlier_discussion": "d1", "latest_turn": "d2"},
                {"adaptive_compaction_decision_model": "inception/mercury-decide:free"},
                api_key=None,
            )
            assert prob is None

    @patch("requests.post")
    @patch("zocrates.agents.common.adaptive_decision.probe_local_laya_health")
    def test_local_laya_dispatch_and_health_check(self, mock_health, mock_post):
        """Dispatches to local Laya without Authorization header when model is local."""
        mock_health.return_value = True
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"answers": {"compaction_warranted": {"noul": 0.91}}}
        mock_post.return_value = mock_resp

        cfg = {
            "adaptive_compaction_decision_model": "local_laya",
            "citation_verification_local_url": "http://127.0.0.1:8005/v1/systemone",
        }
        prob = evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, cfg)
        assert prob == 0.91
        mock_health.assert_called_once_with("http://127.0.0.1:8005/health")
        _, kwargs = mock_post.call_args
        assert "Authorization" not in kwargs["headers"]
        assert kwargs["json"]["model"] == "local_laya"

    @patch("zocrates.agents.common.adaptive_decision.probe_local_laya_health", return_value=False)
    def test_local_laya_fails_open_if_health_fails(self, mock_health):
        """Fails open immediately if local Laya health check fails."""
        cfg = {"adaptive_compaction_decision_model": "local_laya"}
        prob = evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, cfg)
        assert prob is None

    @patch("requests.post")
    def test_timeout_kwarg_passed_to_requests(self, mock_post):
        """Verify configured timeout is explicitly passed to requests.post."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"answers": {"compaction_warranted": {"noul": 0.8}}}
        mock_post.return_value = mock_resp

        cfg = {"adaptive_compaction_timeout_sec": 7.5}
        evaluate_adaptive_compaction_rubric({"earlier_discussion": "d1", "latest_turn": "d2"}, cfg, api_key="k")
        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 7.5

    def test_prompt_instructions_identical_to_calibration_script(self):
        """Drift guard: DECISION_PROMPT_INSTRUCTIONS matches calibration harness exactly."""
        import importlib.util
        from pathlib import Path
        import sys

        script_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "adaptive_compaction"
            / "calibrate_decision_model.py"
        )
        spec = importlib.util.spec_from_file_location(
            "calibrate_script", str(script_path)
        )
        assert spec and spec.loader
        cal_mod = importlib.util.module_from_spec(spec)
        sys.modules["calibrate_script"] = cal_mod
        try:
            spec.loader.exec_module(cal_mod)
            assert DECISION_PROMPT_INSTRUCTIONS == cal_mod.DECISION_PROMPT_INSTRUCTIONS
        finally:
            sys.modules.pop("calibrate_script", None)

    @patch("requests.post")
    def test_opentelemetry_span_created_with_evaluator_kind_and_attributes(self, mock_post):
        """Verify compaction_decision_probe span has kind=EVALUATOR and Phoenix attributes."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "answers": {"compaction_warranted": {"noul": 0.88}}
        }
        mock_post.return_value = mock_resp

        state = {
            "earlier_discussion": "**User**: Q1\n**Assistant**: A1",
            "latest_turn": "**User**: Q2\n**Assistant**: A2",
        }
        cfg = {
            "adaptive_compaction_decision_model": "inception/mercury-decide:free",
            "adaptive_compaction_threshold": 0.75,
        }
        metadata = {
            "completed_turns": 4,
            "coordinator_prompt_tokens": 35000,
            "session_id": "test-session-123",
        }

        with patch.object(trace, "get_tracer", side_effect=provider.get_tracer):
            prob = evaluate_adaptive_compaction_rubric(
                state, cfg, api_key="sk-test", metadata=metadata
            )

        assert prob == 0.88
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "compaction_decision_probe"
        assert span.attributes["openinference.span.kind"] == "EVALUATOR"
        assert span.attributes["compaction.decision.model"] == "inception/mercury-decide:free"
        assert span.attributes["compaction.decision.provider"] == "openrouter"
        assert span.attributes["compaction.decision.probability"] == 0.88
        assert span.attributes["compaction.decision.threshold"] == 0.75
        assert span.attributes["compaction.decision.verdict"] == "COMPRESS"
        assert span.attributes["compaction.decision.status"] == "success"
        assert "metadata" in span.attributes
        meta_dict = json.loads(span.attributes["metadata"])
        assert meta_dict["completed_turns"] == 4
        assert meta_dict["coordinator_prompt_tokens"] == 35000

    def test_telemetry_span_customizer_sets_evaluator_kind(self):
        """PhoenixEnrichmentSpanProcessor in telemetry.py enforces openinference.span.kind = EVALUATOR."""
        from zocrates.utils.telemetry import PhoenixEnrichmentSpanProcessor

        processor = PhoenixEnrichmentSpanProcessor()
        mock_span = MagicMock()
        mock_span.name = "compaction_decision_probe"
        mock_span._attributes = {}
        mock_span.set_attribute = lambda k, v: mock_span._attributes.update({k: v})

        processor.on_end(mock_span)
        assert mock_span._attributes.get("openinference.span.kind") == "EVALUATOR"


# ---------------------------------------------------------------------------
# Test Suite 5: should_trigger_adaptive_compaction
# ---------------------------------------------------------------------------

class TestShouldTriggerAdaptiveCompaction:
    def _make_n_turn_session(self, n: int, prompt_tokens: int = 35000) -> MockSession:
        events = []
        for i in range(1, n + 1):
            events.append(make_user_event(f"Q{i}", invocation_id=f"inv_{i}", timestamp=float(i * 2)))
            events.append(
                make_coordinator_event(
                    f"A{i}",
                    invocation_id=f"inv_{i}",
                    timestamp=float(i * 2 + 1),
                    prompt_tokens=prompt_tokens if i == n else None,
                )
            )
        return MockSession(events=events)

    def test_disabled_toggle_skips_immediately(self):
        """Returns False immediately when enable_adaptive_compaction is False."""
        cfg = {"enable_adaptive_compaction": False}
        session = self._make_n_turn_session(5)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg)
        assert not should_comp
        assert reason == "adaptive_compaction_disabled"

    def test_envelope_gate_skips_when_few_turns(self):
        """Returns False when completed_turns < min_turns."""
        cfg = {"adaptive_compaction_min_turns": 4, "adaptive_compaction_retention_turns": 2}
        session = self._make_n_turn_session(3)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg)
        assert not should_comp
        assert reason == "turn_count_below_min (3 < 4)"

    def test_envelope_gate_skips_when_tokens_below_floor(self):
        """Returns False when prompt tokens < token_floor."""
        cfg = {
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_floor": 30000,
        }
        # 4 turns, but prompt tokens = 18000 < 30000
        session = self._make_n_turn_session(4, prompt_tokens=18000)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg)
        assert not should_comp
        assert reason == "tokens_below_floor (18000 < 30000)"

    def test_hard_token_ceiling_forces_compress_without_probing(self):
        """Tokens >= ceiling bypasses the decision probe entirely and returns True."""
        cfg = {
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_ceiling": 100000,
        }
        # 4 turns, prompt tokens = 105000 >= 100000
        session = self._make_n_turn_session(4, prompt_tokens=105000)
        with patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric") as mock_probe:
            should_comp, reason = should_trigger_adaptive_compaction(session, cfg)
            assert should_comp
            assert reason == "token_ceiling_exceeded (105000 >= 100000)"
            mock_probe.assert_not_called()

    @patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric")
    def test_decision_model_compress_verdict(self, mock_probe):
        """When probe probability >= threshold (0.75), returns True (COMPRESS)."""
        mock_probe.return_value = 0.82
        cfg = {
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_floor": 30000,
            "adaptive_compaction_threshold": 0.75,
        }
        session = self._make_n_turn_session(4, prompt_tokens=35000)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg, api_key="sk-test")
        assert should_comp
        assert reason == "decision_probe_triggered (p=0.820 >= 0.75)"
        mock_probe.assert_called_once()

    @patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric")
    def test_decision_model_continue_verdict(self, mock_probe):
        """When probe probability < threshold (0.75), returns False (CONTINUE)."""
        mock_probe.return_value = 0.55
        cfg = {
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_floor": 30000,
            "adaptive_compaction_threshold": 0.75,
        }
        session = self._make_n_turn_session(4, prompt_tokens=35000)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg, api_key="sk-test")
        assert not should_comp
        assert reason == "decision_probe_declined (p=0.550 < 0.75)"

    @patch("zocrates.agents.common.adaptive_decision.evaluate_adaptive_compaction_rubric")
    def test_probe_failure_fails_open_to_continue(self, mock_probe):
        """When probe fails/times out (returns None), fails open to False."""
        mock_probe.return_value = None
        cfg = {
            "adaptive_compaction_min_turns": 4,
            "adaptive_compaction_token_floor": 30000,
        }
        session = self._make_n_turn_session(4, prompt_tokens=35000)
        should_comp, reason = should_trigger_adaptive_compaction(session, cfg, api_key="sk-test")
        assert not should_comp
        assert reason == "probe_failed_or_timed_out"
