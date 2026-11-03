"""Predictive next-prompt engine: suggests the user's next research prompt."""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from zocrates.config import DEFAULT_PREDICTIVE_PROMPT_MODEL
from zocrates.utils.cost_calculator import calculate_event_cost

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode
except Exception:
    _otel_trace = None
    _StatusCode = None

PREDICTOR_SYSTEM_PROMPT = """# Role and Objective

You are the Next-Move Predictor for Zocrates, an AI research assistant embedded inside Zotero.

Given the recent research turns: predict the single most valuable, actionable, and logical next prompt the user would send to advance their research.

# Rules

- Length: Strictly 5 to 15 words. Single line only.
- Perspective: Direct user voice (an imperative command or query the user would type).
   - Good: "Search online literature for papers on Medusa speculative decoding"
   - Good: "Compare Medusa decoding heads against our library draft models"
   - Good: "Show experimental benchmarks comparing draft models on GSM8K"
   - Bad: "Would you like me to find more papers?" (Do NOT speak as the assistant)
   - Bad: "Can you explain this further?" (Too vague)
- Actionability:
   - If the research response identified coverage gaps in the library, predict an online literature search filling those specific gaps.
   - If candidate papers were just imported or discovered, predict a comparison or methodological breakdown of those papers.
   - If a complex methodology was explained, predict an inquiry into empirical results, tradeoffs, or code implementations.
- Suppression: If the conversation is purely conversational, a courtesy exchange ("Thanks!"), or has reached a natural resting point with no obvious next research move, output EXACTLY: null.
"""

PREDICTOR_TIMEOUT_SECONDS: float = 4.0
MIN_PREDICTED_WORDS: int = 5
MAX_PREDICTED_WORDS: int = 15


def format_research_turn_history_context(
    research_history: list[dict[str, Any]],
) -> str:
    """Format the last 2 research pipeline turns (User Query + Search Goal + Conclude Output).

    Provides deep multi-turn continuity across library search and online discovery
    in an ultra-compact (<200 tokens) structured representation.
    """
    if not research_history:
        return ""

    recent_turns = research_history[-2:]
    blocks: list[str] = []

    for idx, turn in enumerate(recent_turns, 1):
        pipe_label = (
            "Local Library Search"
            if turn.get("pipeline") == "library_search"
            else "Online Literature Discovery"
        )
        lines = [f"### Research Turn {idx} ({pipe_label})"]

        user_q = turn.get("user_query")
        if user_q:
            lines.append(f'- User Inquiry: "{user_q}"')

        search_goal = turn.get("search_goal")
        if search_goal:
            lines.append(f'- Coordinator Search Goal: "{search_goal}"')

        conclude = turn.get("conclude_output") or {}
        summary = conclude.get("summary")
        if summary:
            lines.append(f"- Findings / Evidence: {summary}")

        gaps = conclude.get("gaps")
        if gaps and isinstance(gaps, list):
            valid_gaps = [str(g).strip() for g in gaps if str(g).strip()]
            if valid_gaps:
                gaps_str = "\n  - ".join(valid_gaps)
                lines.append(f"- Literature Gaps: \n{gaps_str}")

        candidates = conclude.get("selected_candidates")
        if candidates and isinstance(candidates, list):
            cand_strs = [
                f"  - Title: {c.get('title')}; Summary: {c.get('tldr')}"
                if c.get("tldr") else f"  - Title: {c.get('title')}"
                for c in candidates
                if isinstance(c, dict) and c.get("title")
            ]
            if cand_strs:
                cands_str = "\n".join(cand_strs)
                lines.append(f"- Discovered Papers: \n{cands_str}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks).strip()


def parse_predicted_prompt(raw_text: str | None) -> str | None:
    """Parse, sanitize, and validate the predicted prompt string."""
    if not raw_text:
        return None

    cleaned = raw_text.strip().strip("\"'").replace("\r", " ").replace("\n", " ").strip()
    cleaned = " ".join(cleaned.split())

    lower = cleaned.lower()
    if lower in ("null", "none", "n/a", "no suggestion", "nil", "") or lower.startswith("null"):
        return None

    words = cleaned.split()
    if len(words) < MIN_PREDICTED_WORDS:
        return None
    if len(words) > MAX_PREDICTED_WORDS:
        cleaned = " ".join(words[:MAX_PREDICTED_WORDS])

    return cleaned


def _safe_set(span: Any, key: str, value: Any) -> None:
    """Safely set an attribute on an active OpenTelemetry span, no-oping on error or None."""
    if span is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception as exc:
        logger.debug("Failed setting span attribute %s: %s", key, exc)


def _start_predictor_span() -> Any:
    """Return a context manager yielding an OpenTelemetry span, or ``None`` on fallback."""
    if _otel_trace is None:
        return contextlib.nullcontext(None)
    try:
        tracer = _otel_trace.get_tracer("zocrates.predictor")
        return tracer.start_as_current_span("predict_next_user_prompt")
    except Exception as exc:
        logger.debug("Failed starting predictor span: %s", exc)
        return contextlib.nullcontext(None)


def _usage_count(usage: Any, name: str) -> int:
    """Read a token count from a litellm usage object or dict, defaulting to 0."""
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def predict_next_user_prompt(
    research_history: list[dict[str, Any]],
    model: str = DEFAULT_PREDICTIVE_PROMPT_MODEL,
    api_key: str | None = None,
    telemetry_out: dict[str, Any] | None = None,
) -> str | None:
    """Execute a direct ``litellm.acompletion`` call to predict the next user prompt.

    Returns ``None`` on empty context, suppression, timeout, or any provider error.
    When ``telemetry_out`` is provided it is cleared and populated with the model,
    latency, token usage, cost, status and suggestion of the call.
    """
    import litellm

    if telemetry_out is not None:
        telemetry_out.clear()

    findings_str = format_research_turn_history_context(research_history)
    if not findings_str:
        return None

    messages: list[dict[str, str]] = [
        {"role": "system", "content": PREDICTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"# Recent Research Turns\n\n{findings_str}\n\n"
                "---\n\n"
                "Predict the single most logical next prompt for the user (5-15 words):"
            ),
        },
    ]

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 40,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if model.startswith("openrouter/"):
        kwargs["extra_body"] = {"reasoning": {"effort": "none"}}

    suggestion: str | None = None
    raw_choice: str | None = None
    prompt_tokens = comp_tokens = total_tokens = 0
    cost_usd = 0.0
    status = "error"
    start = time.perf_counter()

    with _start_predictor_span() as span:
        _safe_set(span, "openinference.span.kind", "LLM")
        _safe_set(span, "input.value", f"{PREDICTOR_SYSTEM_PROMPT}\n\nRecent research turns:\n\n{findings_str}")
        _safe_set(span, "input.mime_type", "text/plain")
        _safe_set(span, "llm.model_name", model)
        _safe_set(span, "llm.invocation_parameters", json.dumps({"model": model, "temperature": 0.1, "max_tokens": 40}))
        _safe_set(span, "gen_ai.system", "openrouter" if model.startswith("openrouter/") else "other")
        _safe_set(span, "gen_ai.request.model", model)
        _safe_set(span, "gen_ai.request.temperature", 0.1)
        _safe_set(span, "gen_ai.request.max_tokens", 40)
        _safe_set(span, "predictive_prompt.research_turns_count", len(research_history))

        try:
            response = await asyncio.wait_for(
                litellm.acompletion(**kwargs),
                timeout=PREDICTOR_TIMEOUT_SECONDS,
            )
            usage = getattr(response, "usage", None)
            if usage:
                prompt_tokens = _usage_count(usage, "prompt_tokens")
                comp_tokens = _usage_count(usage, "completion_tokens")
                total_tokens = _usage_count(usage, "total_tokens")
                try:
                    cost_usd = float(calculate_event_cost(model, prompt_tokens, comp_tokens))
                except Exception as cost_exc:
                    logger.debug("Predictor cost calculation failed: %s", cost_exc)
                _safe_set(span, "gen_ai.usage.input_tokens", prompt_tokens)
                _safe_set(span, "gen_ai.usage.output_tokens", comp_tokens)
                _safe_set(span, "gen_ai.usage.total_tokens", total_tokens)
                _safe_set(span, "gen_ai.cost", cost_usd)
            raw_choice = response.choices[0].message.content
            _safe_set(span, "output.value", raw_choice or "")
            _safe_set(span, "output.mime_type", "text/plain")
            _safe_set(span, "llm.token_count.prompt", prompt_tokens)
            _safe_set(span, "llm.token_count.completion", comp_tokens)
            _safe_set(span, "llm.token_count.total", total_tokens)
            suggestion = parse_predicted_prompt(raw_choice)
            status = "success" if suggestion else ("suppressed" if raw_choice else "error")
            if _StatusCode is not None and span is not None:
                try:
                    span.set_status(_StatusCode.OK)
                except Exception as exc:
                    logger.debug("Failed setting span status: %s", exc)
        except Exception as exc:
            suggestion = None
            status = "timeout" if isinstance(exc, asyncio.TimeoutError) else "error"
            logger.warning("Predictive next-prompt generation timed out or failed: %s", exc)
            if span is not None:
                try:
                    span.record_exception(exc)
                    if _StatusCode is not None:
                        span.set_status(_StatusCode.ERROR, str(exc))
                except Exception as span_exc:
                    logger.debug("Failed recording span exception: %s", span_exc)

        latency_ms = (time.perf_counter() - start) * 1000.0
        _safe_set(span, "gen_ai.latency_ms", round(latency_ms, 2))
        _safe_set(span, "predictive_prompt.result", suggestion if suggestion is not None else "")

    if telemetry_out is not None:
        telemetry_out.update(
            {
                "model": model,
                "latency_ms": round(latency_ms, 2),
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": comp_tokens,
                    "total_tokens": total_tokens,
                },
                "cost_usd": round(cost_usd, 6),
                "status": status,
                "suggested_prompt": suggestion,
            }
        )
    return suggestion
