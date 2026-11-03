"""Unit tests for app.agents.coordinator.predictor (predictive next-prompt engine)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zocrates.agents.coordinator.predictor import (
    PREDICTOR_SYSTEM_PROMPT,
    format_research_turn_history_context,
    parse_predicted_prompt,
    predict_next_user_prompt,
)

LIBRARY_TURN = {
    "invocation_id": "inv-1",
    "pipeline": "library_search",
    "user_query": "What does my library say about speculative decoding?",
    "search_goal": "speculative decoding draft models",
    "conclude_output": {
        "summary": "Library covers draft-model speculation only.",
        "gaps": ["Medusa heads", " "],
        "notes": None,
    },
}
ONLINE_TURN = {
    "invocation_id": "inv-2",
    "pipeline": "online_search",
    "user_query": "Find papers on Medusa",
    "search_goal": "Medusa speculative decoding",
    "conclude_output": {
        "summary": "Found 3 candidates.",
        "gaps": [],
        "selected_candidates": [
            {"title": "Medusa", "tldr": "Multiple decoding heads."},
            {"title": "EAGLE", "tldr": ""},
            {"title": "", "tldr": "ignored"},
        ],
    },
}


def test_format_context_empty_history():
    assert format_research_turn_history_context([]) == ""


def test_format_context_uses_only_last_two_turns():
    older = dict(LIBRARY_TURN, user_query="OLDEST TURN")
    text = format_research_turn_history_context([older, LIBRARY_TURN, ONLINE_TURN])
    assert "OLDEST TURN" not in text
    assert "Research Turn 1 (Local Library Search)" in text
    assert "Research Turn 2 (Online Literature Discovery)" in text
    assert "Literature Gaps:" in text
    assert "Medusa heads" in text
    assert "Discovered Papers:" in text
    assert "  - Title: Medusa; Summary: Multiple decoding heads." in text
    assert "  - Title: EAGLE" in text
    assert "ignored" not in text


def test_format_context_is_compact():
    text = format_research_turn_history_context([LIBRARY_TURN, ONLINE_TURN])
    assert len(text.split()) < 200


@pytest.mark.parametrize("raw", [None, "", "   ", "null", "None", "N/A", "nil", "No suggestion", "null."])
def test_parse_null_variants(raw):
    assert parse_predicted_prompt(raw) is None


def test_parse_strips_quotes_and_collapses_whitespace():
    assert (
        parse_predicted_prompt('  "Compare Medusa  heads\nagainst draft models"  ')
        == "Compare Medusa heads against draft models"
    )


def test_parse_word_count_boundaries():
    assert parse_predicted_prompt("one two three four") is None
    assert parse_predicted_prompt("one two three four five") == "one two three four five"
    fifteen = " ".join(f"w{i}" for i in range(15))
    assert parse_predicted_prompt(fifteen) == fifteen
    twenty = " ".join(f"w{i}" for i in range(20))
    assert parse_predicted_prompt(twenty) == fifteen


def _fake_response(content, usage=None):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    if usage is not None:
        response.usage = SimpleNamespace(**usage)
    return response


USAGE = {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112}
SUGGESTION = "Compare Medusa heads against our draft models"


def test_predict_empty_history_skips_llm():
    with patch("litellm.acompletion", new=AsyncMock()) as mock_call:
        assert asyncio.run(predict_next_user_prompt([])) is None
    mock_call.assert_not_called()


def test_predict_openrouter_call_arguments():
    mock_call = AsyncMock(return_value=_fake_response("Compare Medusa heads against our draft models"))
    with patch("litellm.acompletion", new=mock_call):
        result = asyncio.run(predict_next_user_prompt([LIBRARY_TURN], api_key="sk-test"))
    assert result == "Compare Medusa heads against our draft models"
    kwargs = mock_call.call_args.kwargs
    assert kwargs["model"] == "openrouter/~deepseek/deepseek-flash-latest"
    assert kwargs["temperature"] == 0.1
    assert kwargs["max_tokens"] == 40
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["extra_body"] == {"reasoning": {"effort": "none"}}


def test_predict_non_openrouter_model_omits_reasoning_body():
    mock_call = AsyncMock(return_value=_fake_response("Compare Medusa heads against our draft models"))
    with patch("litellm.acompletion", new=mock_call):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN], model="ollama/llama3"))
    assert "extra_body" not in mock_call.call_args.kwargs
    assert "api_key" not in mock_call.call_args.kwargs


def test_predict_error_returns_none():
    with patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert asyncio.run(predict_next_user_prompt([LIBRARY_TURN])) is None


def test_predict_timeout_returns_none_quickly():
    async def hang(**_kwargs):
        await asyncio.sleep(10)

    async def run():
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await predict_next_user_prompt([LIBRARY_TURN])
        return result, loop.time() - start

    with patch("litellm.acompletion", new=hang):
        result, elapsed = asyncio.run(run())
    assert result is None
    assert elapsed < 6.0


def test_predict_telemetry_out_populated_on_success():
    telemetry = {"stale": True}
    mock_call = AsyncMock(return_value=_fake_response(SUGGESTION, USAGE))
    with (
        patch("litellm.acompletion", new=mock_call),
        patch("zocrates.agents.coordinator.predictor.calculate_event_cost", return_value=0.0001234567) as cost,
    ):
        result = asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry))
    assert result == SUGGESTION
    cost.assert_called_once_with("openrouter/~deepseek/deepseek-flash-latest", 100, 12)
    assert "stale" not in telemetry
    assert telemetry["model"] == "openrouter/~deepseek/deepseek-flash-latest"
    assert telemetry["usage"] == USAGE
    assert telemetry["cost_usd"] == 0.000123
    assert telemetry["status"] == "success"
    assert telemetry["suggested_prompt"] == SUGGESTION
    assert telemetry["latency_ms"] >= 0


def test_predict_telemetry_out_suppressed_and_error_status():
    telemetry: dict = {}
    with patch("litellm.acompletion", new=AsyncMock(return_value=_fake_response("null", USAGE))):
        assert asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry)) is None
    assert telemetry["status"] == "suppressed"
    assert telemetry["suggested_prompt"] is None

    with patch("litellm.acompletion", new=AsyncMock(return_value=_fake_response("", USAGE))):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry))
    assert telemetry["status"] == "error"


def test_predict_telemetry_out_without_usage_defaults_to_zero():
    telemetry: dict = {}
    response = _fake_response(SUGGESTION)
    response.usage = None
    with patch("litellm.acompletion", new=AsyncMock(return_value=response)):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry))
    assert telemetry["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert telemetry["cost_usd"] == 0.0


def test_predict_telemetry_out_on_error_and_timeout():
    telemetry: dict = {}
    with patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry)) is None
    assert telemetry["status"] == "error"
    assert telemetry["suggested_prompt"] is None

    async def hang(**_kwargs):
        await asyncio.sleep(10)

    with (
        patch("litellm.acompletion", new=hang),
        patch("zocrates.agents.coordinator.predictor.PREDICTOR_TIMEOUT_SECONDS", 0.01),
    ):
        assert asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry)) is None
    assert telemetry["status"] == "timeout"
    assert telemetry["usage"]["total_tokens"] == 0


def test_predict_empty_history_clears_telemetry_out():
    telemetry = {"stale": True}
    assert asyncio.run(predict_next_user_prompt([], telemetry_out=telemetry)) is None
    assert telemetry == {}


def test_predict_creates_otel_span_with_genai_attributes():
    span = MagicMock()
    tracer = MagicMock()
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    otel = MagicMock()
    otel.get_tracer.return_value = tracer
    status_code = MagicMock()
    with (
        patch("litellm.acompletion", new=AsyncMock(return_value=_fake_response(SUGGESTION, USAGE))),
        patch("zocrates.agents.coordinator.predictor.calculate_event_cost", return_value=0.5),
        patch("zocrates.agents.coordinator.predictor._otel_trace", otel),
        patch("zocrates.agents.coordinator.predictor._StatusCode", status_code),
    ):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN, ONLINE_TURN]))
    otel.get_tracer.assert_called_once_with("zocrates.predictor")
    tracer.start_as_current_span.assert_called_once_with("predict_next_user_prompt")
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["gen_ai.system"] == "openrouter"
    assert attrs["gen_ai.request.model"] == "openrouter/~deepseek/deepseek-flash-latest"
    assert attrs["gen_ai.request.temperature"] == 0.1
    assert attrs["gen_ai.request.max_tokens"] == 40
    assert attrs["predictive_prompt.research_turns_count"] == 2
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 12
    assert attrs["gen_ai.usage.total_tokens"] == 112
    assert attrs["gen_ai.cost"] == 0.5
    assert attrs["predictive_prompt.result"] == SUGGESTION
    assert attrs["gen_ai.latency_ms"] >= 0
    assert attrs["openinference.span.kind"] == "LLM"
    assert PREDICTOR_SYSTEM_PROMPT in attrs["input.value"]
    assert attrs["input.mime_type"] == "text/plain"
    assert attrs["output.value"] == SUGGESTION
    assert attrs["output.mime_type"] == "text/plain"
    assert attrs["llm.model_name"] == "openrouter/~deepseek/deepseek-flash-latest"
    assert json.loads(attrs["llm.invocation_parameters"]) == {
        "model": "openrouter/~deepseek/deepseek-flash-latest",
        "temperature": 0.1,
        "max_tokens": 40,
    }
    assert attrs["llm.token_count.prompt"] == 100
    assert attrs["llm.token_count.completion"] == 12
    assert attrs["llm.token_count.total"] == 112
    span.set_status.assert_called_once_with(status_code.OK)


def test_predict_otel_span_records_exception_and_other_system():
    span = MagicMock()
    tracer = MagicMock()
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    otel = MagicMock()
    otel.get_tracer.return_value = tracer
    status_code = MagicMock()
    boom = RuntimeError("boom")
    with (
        patch("litellm.acompletion", new=AsyncMock(side_effect=boom)),
        patch("zocrates.agents.coordinator.predictor._otel_trace", otel),
        patch("zocrates.agents.coordinator.predictor._StatusCode", status_code),
    ):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN], model="ollama/llama3"))
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["gen_ai.system"] == "other"
    span.record_exception.assert_called_once_with(boom)
    span.set_status.assert_called_once_with(status_code.ERROR, "boom")


def test_predict_works_without_opentelemetry():
    telemetry: dict = {}
    with (
        patch("litellm.acompletion", new=AsyncMock(return_value=_fake_response(SUGGESTION, USAGE))),
        patch("zocrates.agents.coordinator.predictor._otel_trace", None),
    ):
        result = asyncio.run(predict_next_user_prompt([LIBRARY_TURN], telemetry_out=telemetry))
    assert result == SUGGESTION
    assert telemetry["status"] == "success"


def test_predict_real_otel_sdk_span_attributes():
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_otel = MagicMock()
    real_otel.get_tracer.side_effect = lambda name: provider.get_tracer(name)
    with (
        patch("litellm.acompletion", new=AsyncMock(return_value=_fake_response(SUGGESTION, USAGE))),
        patch("zocrates.agents.coordinator.predictor._otel_trace", real_otel),
    ):
        asyncio.run(predict_next_user_prompt([LIBRARY_TURN]))
    (finished,) = exporter.get_finished_spans()
    assert finished.name == "predict_next_user_prompt"
    assert finished.attributes["gen_ai.usage.total_tokens"] == 112
    assert finished.attributes["gen_ai.system"] == "openrouter"
    assert finished.attributes["predictive_prompt.result"] == SUGGESTION
    assert finished.status.status_code.name == "OK"
    assert finished.attributes["openinference.span.kind"] == "LLM"
    assert PREDICTOR_SYSTEM_PROMPT in finished.attributes["input.value"]
    assert finished.attributes["output.value"] == SUGGESTION
    assert finished.attributes["llm.model_name"] == "openrouter/~deepseek/deepseek-flash-latest"
    assert finished.attributes["llm.token_count.prompt"] == 100
    assert finished.attributes["llm.token_count.completion"] == 12
    assert finished.attributes["llm.token_count.total"] == 112
