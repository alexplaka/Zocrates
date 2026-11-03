"""Unit tests for the OpenTelemetry and Arize Phoenix telemetry setup."""

import os
from unittest.mock import MagicMock, patch

import pytest

from zocrates.utils.telemetry import (
    PhoenixEnrichmentSpanProcessor,
    _format_parts_with_labeled_thoughts,
    _is_port_open,
    _patch_adk_semconv_to_part,
    _set_span_attribute,
    _summarize_merged_tool_response,
    flush_telemetry,
    get_active_telemetry_endpoint,
    setup_telemetry,
)


def test_is_port_open() -> None:
    """Test localhost port probing helper."""
    # A port that should definitely not be open
    assert not _is_port_open("127.0.0.1", 59999, timeout=0.05)


def test_flush_telemetry_safe_execution() -> None:
    """flush_telemetry should safely invoke force_flush without throwing."""
    mock_provider = MagicMock()
    mock_provider.force_flush.return_value = True

    with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_provider):
        flush_telemetry(timeout_millis=1000)
        mock_provider.force_flush.assert_called_once_with(1000)


def test_setup_telemetry_idempotent() -> None:
    """Subsequent calls to setup_telemetry should be idempotent."""
    with patch("zocrates.utils.telemetry._telemetry_configured", True), \
         patch("zocrates.utils.telemetry._active_telemetry_endpoint", "http://localhost:6006/v1/traces"):
        res = setup_telemetry()
        assert res == "http://localhost:6006/v1/traces"


def test_setup_telemetry_explicit_disabled(monkeypatch) -> None:
    """When LOCAL_TRACING=false and no bucket or local OTLP endpoint, tracing is skipped."""
    monkeypatch.setenv("LOCAL_TRACING", "false")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)

    with patch("zocrates.utils.telemetry._telemetry_configured", False), \
         patch("zocrates.utils.telemetry._active_telemetry_endpoint", None):
        res = setup_telemetry()
        assert res is None
        assert get_active_telemetry_endpoint() is None


def test_phoenix_enrichment_span_processor_tool_span() -> None:
    """Processor should enrich tool execution spans with OpenInference attributes."""
    processor = PhoenixEnrichmentSpanProcessor()
    mock_span = MagicMock()
    mock_span.name = "execute_tool search_corpus"
    mock_span._attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gcp.vertex.agent.tool_call_args": '{"query": "coding agents"}',
        "gcp.vertex.agent.tool_response": '{"status": "ok", "results": [1, 2]}',
    }

    processor.on_end(mock_span)

    assert mock_span._attributes["openinference.span.kind"] == "TOOL"
    assert mock_span._attributes["input.value"] == '{"query": "coding agents"}'
    assert mock_span._attributes["tool.parameters"] == '{"query": "coding agents"}'
    assert mock_span._attributes["output.value"] == '{"status": "ok", "results": [1, 2]}'


def test_phoenix_enrichment_span_processor_llm_span() -> None:
    """Processor should enrich LLM spans with input.value and output.value."""
    processor = PhoenixEnrichmentSpanProcessor()
    mock_span = MagicMock()
    mock_span.name = "generate_content openrouter/model"
    mock_span._attributes = {
        "gen_ai.operation.name": "generate_content",
        "gen_ai.input.messages": '[{"role": "user", "parts": [{"type": "text", "content": "Hello!"}]}]',
        "gen_ai.output.messages": '[{"role": "assistant", "parts": [{"type": "text", "content": "Hi there!"}]}]',
    }

    processor.on_end(mock_span)

    assert mock_span._attributes["openinference.span.kind"] == "LLM"
    assert mock_span._attributes["input.value"] == "[user] Hello!"
    assert mock_span._attributes["output.value"] == "Hi there!"


@pytest.mark.parametrize(
    ("span_name", "extra_attrs"),
    [
        ("predict_next_user_prompt", {}),
        ("custom_span", {"openinference.span.kind": "LLM"}),
    ],
)
def test_phoenix_enrichment_span_processor_predictor_span_preserves_io(
    span_name: str, extra_attrs: dict[str, str]
) -> None:
    """Predictor LLM spans are tagged LLM and keep their pre-set input/output values."""
    processor = PhoenixEnrichmentSpanProcessor()
    mock_span = MagicMock()
    mock_span.name = span_name
    mock_span._attributes = {
        "input.value": "research history context",
        "output.value": "Search online literature for Medusa decoding papers",
        **extra_attrs,
    }

    processor.on_end(mock_span)

    assert mock_span._attributes["openinference.span.kind"] == "LLM"
    assert mock_span._attributes["input.value"] == "research history context"
    assert mock_span._attributes["output.value"] == "Search online literature for Medusa decoding papers"


def test_format_parts_with_labeled_thoughts() -> None:
    """_format_parts_with_labeled_thoughts cleanly separates thoughts from answers."""
    parts = [
        {"type": "text", "content": "Here is a thinking process: analyzing user question."},
        {"type": "text", "content": "The framework operates in a continuous loop."},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert "### 💭 Thought Process" in output_value
    assert "Here is a thinking process: analyzing user question." in output_value
    assert "---" in output_value
    assert "### 💬 Response" in output_value
    assert "The framework operates in a continuous loop." in output_value

    assert len(enriched_parts) == 2
    assert enriched_parts[0]["content"].startswith("### 💭 Thought Process")
    assert enriched_parts[1]["content"].startswith("### 💬 Response")


def test_format_parts_with_think_tags() -> None:
    """_format_parts_with_labeled_thoughts extracts <think>...</think> tags."""
    parts = [
        {"type": "text", "content": "<think>Internal reasoning step</think>Final answer for user."}
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert "### 💭 Thought Process" in output_value
    assert "Internal reasoning step" in output_value
    assert "### 💬 Response" in output_value
    assert "Final answer for user." in output_value


def test_format_parts_with_tool_call() -> None:
    """_format_parts_with_labeled_thoughts cleanly handles thought followed by tool call."""
    parts = [
        {"type": "text", "content": "We need to invoke library search."},
        {"type": "tool_call", "name": "library_search", "arguments": {"query": "test"}},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert "### 💭 Thought Process" in output_value
    assert "We need to invoke library search." in output_value
    assert "⚙️ **Tool Call:** `library_search`" in output_value


def test_format_parts_sse_streaming_text_chunks() -> None:
    """A list of streaming text chunks should produce exactly 1 contiguous response part and no thoughts."""
    parts = [
        {"type": "text", "content": "The "},
        {"type": "text", "content": "framework "},
        {"type": "text", "content": "works."},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert len(enriched_parts) == 1
    assert enriched_parts[0]["content"] == "The framework works."
    assert "Thought Process" not in output_value
    assert "Response" not in output_value
    assert output_value == "The framework works."


def test_format_parts_sse_streaming_with_thoughts() -> None:
    """Streaming chunks with thoughts followed by response should produce 2 contiguous parts."""
    parts = [
        {"type": "text", "content": "Thinking ", "thought": True},
        {"type": "text", "content": "about architecture...", "thought": True},
        {"type": "text", "content": "The "},
        {"type": "text", "content": "answer "},
        {"type": "text", "content": "is simple."},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert len(enriched_parts) == 2
    assert enriched_parts[0]["content"] == "### 💭 Thought Process\n\nThinking about architecture..."
    assert enriched_parts[1]["content"] == "### 💬 Response\n\nThe answer is simple."
    assert "---" in output_value
    assert "Thinking about architecture..." in output_value
    assert "The answer is simple." in output_value


def test_format_parts_sse_streaming_think_tags_across_chunks() -> None:
    """<think> tags split across streaming chunks should be cleanly extracted into contiguous parts."""
    parts = [
        {"type": "text", "content": "<think>Deliberating "},
        {"type": "text", "content": "on the methodology</think>Here "},
        {"type": "text", "content": "is the synthesis."},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert len(enriched_parts) == 2
    assert enriched_parts[0]["content"] == "### 💭 Thought Process\n\nDeliberating on the methodology"
    assert enriched_parts[1]["content"] == "### 💬 Response\n\nHere is the synthesis."
    assert "---" in output_value


def test_format_parts_ordinary_prose_containing_thinking_and_response() -> None:
    """Ordinary prose containing words like 'thinking' and 'response' must not trigger thought splitting."""
    parts = [
        {"type": "text", "content": "I am thinking about this question, "},
        {"type": "text", "content": "and my response is clear."},
    ]

    output_value, enriched_parts = _format_parts_with_labeled_thoughts(parts)

    assert len(enriched_parts) == 1
    assert enriched_parts[0]["content"] == "I am thinking about this question, and my response is clear."
    assert "Thought Process" not in output_value
    assert "### 💬 Response" not in output_value
    assert output_value == "I am thinking about this question, and my response is clear."


def test_patch_adk_semconv_to_part_preserves_thoughts() -> None:
    """_patch_adk_semconv_to_part should preserve thought=True on emitted message parts."""
    import google.adk.telemetry._experimental_semconv as semconv
    from google.genai import types

    _patch_adk_semconv_to_part()

    thought_part = types.Part(text="Analyzing paper structure...", thought=True)
    text_part = types.Part(text="Here is the synthesis.", thought=False)

    msg = semconv._to_input_message(types.Content(role="model", parts=[thought_part, text_part]))
    assert len(msg["parts"]) == 2
    assert msg["parts"][0].get("thought") is True
    assert msg["parts"][0]["content"] == "Analyzing paper structure..."
    assert not msg["parts"][1].get("thought")
    assert msg["parts"][1]["content"] == "Here is the synthesis."


def test_phoenix_enrichment_span_processor_consolidates_input_messages() -> None:
    """Span processor must consolidate streaming tokens in input.messages instead of repeating per token."""
    import json

    processor = PhoenixEnrichmentSpanProcessor()
    mock_span = MagicMock()
    mock_span.name = "generate_content openrouter/model"

    input_messages = [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "Hello, "},
                {"type": "text", "content": "can you search?"},
            ],
        },
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Thinking ", "thought": True},
                {"type": "text", "content": "about query...", "thought": True},
                {"type": "text", "content": "I "},
                {"type": "text", "content": "will "},
                {"type": "text", "content": "search."},
            ],
        },
    ]

    mock_span._attributes = {
        "gen_ai.operation.name": "generate_content",
        "gen_ai.input.messages": json.dumps(input_messages),
    }

    processor.on_end(mock_span)

    # input.value should NOT repeat [assistant] per token
    input_val = mock_span._attributes.get("input.value", "")
    assert input_val.count("[assistant]") == 1
    assert input_val.count("[user]") == 1
    assert "Thinking about query..." in input_val
    assert "I will search." in input_val

    # gen_ai.input.messages should be consolidated
    updated_input_msgs = json.loads(mock_span._attributes["gen_ai.input.messages"])
    assert len(updated_input_msgs) == 2
    # User message text consolidated into 1 part
    assert len(updated_input_msgs[0]["parts"]) == 1
    assert updated_input_msgs[0]["parts"][0]["content"] == "Hello, can you search?"
    # Assistant message consolidated into 2 parts: 1 thought + 1 response
    assert len(updated_input_msgs[1]["parts"]) == 2
    assert updated_input_msgs[1]["parts"][0]["content"].startswith("### 💭 Thought Process")
    assert updated_input_msgs[1]["parts"][1]["content"].startswith("### 💬 Response")


def test_phoenix_enrichment_span_processor_merged_tool_span() -> None:
    """execute_tool (merged) spans must suppress multi-MB state_delta bloat from output.value."""
    import json

    processor = PhoenixEnrichmentSpanProcessor()
    mock_span = MagicMock()
    mock_span.name = "execute_tool (merged)"

    raw_event_with_bloat = {
        "content": {
            "parts": [
                {
                    "function_response": {
                        "id": "call_1",
                        "name": "search_corpus",
                        "response": {
                            "status": "success",
                            "results": [{"chunk_id": "X::1", "text": "huge paper text " * 100}],
                            "retrieval_status": "[Retrieval status: 1 new chunk retrieved]",
                        },
                    }
                }
            ],
            "role": "user",
        },
        "actions": {
            "state_delta": {
                "temp:retrieved_evidence": [{"huge": "state " * 1000}],
            }
        },
    }

    mock_span._attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gcp.vertex.agent.tool_response": json.dumps(raw_event_with_bloat),
    }

    processor.on_end(mock_span)

    out_val = mock_span._attributes.get("output.value", "")
    assert "state_delta" not in out_val
    assert "temp:retrieved_evidence" not in out_val
    parsed_out = json.loads(out_val)
    assert "merged_tools" in parsed_out
    assert parsed_out["merged_tools"][0]["tool"] == "search_corpus"
    assert parsed_out["merged_tools"][0]["status"] == "success"


