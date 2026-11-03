"""Google ADK 2.8+ session context compaction utilities for Zocrates.

Provides fail-open event summarization, pipeline isolation scope filtering,
and thought-stripped markdown formatting for dialogue history.
"""

import asyncio
from collections.abc import Iterator
import contextlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode
except ImportError:
    _otel_trace = None
    _StatusCode = None

from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events import Event
from google.adk.events.event_actions import EventActions, EventCompaction
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai.types import Content, Part

from zocrates.llm import build_agent_llm
from zocrates.config import (
    DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
    DEFAULT_COMPACTION_SUMMARIZER_MODEL,
    DEFAULT_ENABLE_ADAPTIVE_COMPACTION,
    load_zocrates_config,
)
from zocrates.agents.common.adaptive_decision import (
    _is_compaction_event,
    _safe_apply_rewinds,
    should_trigger_adaptive_compaction,
)

logger = logging.getLogger(__name__)


def _safe_set(span: Any, key: str, value: Any) -> None:
    """Safely set an attribute on an active OpenTelemetry span, no-oping on error or None."""
    if span is None:
        return
    try:
        if hasattr(span, "set_attribute"):
            if isinstance(value, (dict, list)):
                span.set_attribute(key, json.dumps(value, ensure_ascii=False))
            else:
                span.set_attribute(key, value)
        elif hasattr(span, "_attributes") and isinstance(span._attributes, dict):
            span._attributes[key] = value
    except Exception as exc:
        logger.debug("Failed setting span attribute %s: %s", key, exc)


@contextlib.contextmanager
def _start_compaction_span(name: str = "compact_session_history") -> Iterator[Any]:
    """Return a context manager yielding an OpenTelemetry span for Phoenix observability, or None on fallback.

    Args:
        name: Name of the OpenTelemetry span.

    Yields:
        Active span if tracing is available, otherwise None.
    """
    if _otel_trace is None:
        yield None
        return
    span_cm = None
    try:
        tracer = _otel_trace.get_tracer("zocrates.compaction")
        span_cm = tracer.start_as_current_span(name)
    except Exception as exc:
        logger.debug("Failed starting compaction span: %s", exc)
        yield None
        return

    span = None
    if hasattr(span_cm, "__enter__") and hasattr(span_cm, "__exit__"):
        try:
            with span_cm as s:
                span = s
                yield span
        except Exception as exc:
            if span is not None and _StatusCode is not None:
                try:
                    span.set_status(_StatusCode.ERROR, str(exc))
                except Exception:
                    pass
            raise
    else:
        yield span_cm


def _get_active_turn_invocation_id(events: list[Event]) -> str | None:
    """Find the invocation_id of the active turn (the latest non-compaction invocation)."""
    for e in reversed(events):
        if e.invocation_id and not (getattr(e, "actions", None) and getattr(e.actions, "compaction", None)):
            return e.invocation_id
    return None


def _trim_to_completed_turns(events: list[Event]) -> list[Event]:
    """Ensure events to compact only contain fully completed turns.

    In conversational agent workflows, a turn may have 2 events (chat), 3 events (tool call
    + tool response), or more. If raw event slicing leaves an incomplete turn at the tail
    (such as an unanswered user question or open tool call), drop that turn completely
    from the compaction slice so the summarizer prompt always terminates cleanly on an
    Assistant completion.
    """
    if not events:
        return []

    invocations: dict[str, list[Event]] = {}
    inv_order: list[str] = []
    for e in events:
        inv_id = e.invocation_id or "unknown"
        if inv_id not in invocations:
            invocations[inv_id] = []
            inv_order.append(inv_id)
        invocations[inv_id].append(e)

    valid_inv_ids = []
    for inv_id in inv_order:
        inv_events = invocations[inv_id]
        has_assistant_completion = any(
            e.author != "user" and (
                (e.content and any(p.text for p in e.content.parts if not getattr(p, "thought", False)))
                or any(getattr(p, "function_response", None) for p in (e.content.parts if e.content else []))
            )
            for e in inv_events
        )
        if has_assistant_completion:
            valid_inv_ids.append(inv_id)

    result: list[Event] = []
    for inv_id in valid_inv_ids:
        result.extend(invocations[inv_id])
    return result


ZOCRATES_COMPACTION_PROMPT_TEMPLATE = """## OBJECTIVE
You are compacting the prior research dialogue history between a researcher and Zocrates (an AI academic research assistant).

Your objective is to generate an accurate, dense, and grounded synthesis/summary of past dialogue turns so future research turns maintain complete context.

## INSTRUCTIONS

### Sections
You summary must include the following sections:
- Sources: List all papers, studies, and documents discussed in the conversation using unnumbered bullet points with authors, publication year, paper title, and journal/source (e.g. "- Smith et al. (2021) — Mechanisms of BRCA-mediated repair (Nature Genetics)"). Do NOT use numeric citation brackets ([1], [2]) or numbered lists for sources.
- Key Findings & Established Decisions: Summarize core findings, established facts, methodologies, and user decisions in dense bullet points. Attribute findings directly to the authors (e.g. "Petersen et al. (2022) showed...").
- Unresolved Questions & Next Steps: Note any explicit knowledge gaps, ongoing literature inquiries, user preferences, or next steps identified in the conversation.

### Critical Guidelines:
- Exclude Internal Mechanics: Do not describe internal tool calls, retrieval loops, or intermediate system steps; focus strictly on final evidence obtained and conceptual conclusions.
- Conciseness & Dense Structure: Keep the summary dense, direct, and under 400 words. Use structured bullet points rather than verbose paragraphs.
- Do NOT include conversational pleasantries, greeting filler, or language declarations. 
- Do NOT attempt to answer any user questions found in the history. Your task is strictly to summarize the past dialogue into the sections above.

## CONVERSATION HISTORY TO SUMMARIZE

<dialogue_history>

{conversation_history}

</dialogue_history>

## SUMMARY
Now, provide the dense, structured academic compaction summary of the dialogue history above:
"""


class FailOpenLlmEventSummarizer(LlmEventSummarizer):
    """Event summarizer with fail-open resilience and pipeline isolation scope filtering."""

    # Raised ceiling (30,000 chars ~6,000 tokens) to ensure full academic syntheses,
    # comparison tables, and trailing `# Cited Sources` bibliographies are never clipped.
    _MAX_TOOL_CONTENT_CHARS = 30000

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Initialize fail-open LLM event summarizer.

        Args:
            *args: Positional arguments forwarded to `LlmEventSummarizer`.
            **kwargs: Keyword arguments forwarded to `LlmEventSummarizer`.
        """
        super().__init__(*args, **kwargs)
        self._last_compacted_turn_inv_id: str | None = None

    def _truncate(self, text: str) -> str:
        """Caps `text` while guaranteeing trailing `# Cited Sources`, `# References`, and `# Discovered Candidate Papers` sections are preserved."""
        limit = self._MAX_TOOL_CONTENT_CHARS
        if len(text) <= limit:
            return text

        sources_match = re.search(
            r"(?:\n{1,3}|^\s*)#{1,3}\s*(?:Cited Sources|References|Discovered Candidate Papers)[\s\S]*$",
            text.rstrip("'\"}\n\r\t "),
            flags=re.IGNORECASE,
        )
        if sources_match:
            sources_block = sources_match.group(0).strip()
            prefix_limit = max(1000, limit - len(sources_block) - 50)
            prefix = text[:prefix_limit]
            return f"{prefix}... [truncated {len(text) - len(prefix) - len(sources_block)} chars]\n\n{sources_block}"

        return f"{text[:limit]}... [truncated {len(text) - limit} chars]"

    def _format_events_for_prompt(self, events: list[Event]) -> str:
        """Format dialogue events into clean markdown, strictly omitting thoughts."""
        blocks: list[str] = []
        for event in events:
            if not (event.content and event.content.parts):
                continue

            for part in event.content.parts:
                # 1. Strictly omit internal coordinator thoughts from compaction history
                if getattr(part, "thought", False):
                    continue

                # 2. Text parts: Map speaker to bold Markdown
                text = getattr(part, "text", None)
                if text and str(text).strip():
                    cleaned_text = str(text).strip()
                    speaker = "**User:**" if (event.author == "user" or (event.content and event.content.role == "user")) else "**Assistant:**"
                    blocks.append(f"{speaker}\n{cleaned_text}")

                # Inline media / image snippet representation in compaction summary
                if getattr(part, "inline_data", None):
                    mime = getattr(part.inline_data, "mime_type", "image")
                    blocks.append(f"**User Attached:** [Visual Image Snippet ({mime})]")

                # 3. Tool call
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    fc_name = getattr(fc, "name", "tool")
                    fc_args = getattr(fc, "args", {})
                    args_str = self._truncate(str(fc_args))
                    blocks.append(f"**Tool Call (`{fc_name}`):**\n{args_str}")

                # 4. Tool response
                if getattr(part, "function_response", None):
                    fr = part.function_response
                    fr_name = getattr(fr, "name", "tool")
                    raw_resp = getattr(fr, "response", "")
                    if isinstance(raw_resp, dict):
                        resp_text = (
                            raw_resp.get("result")
                            or raw_resp.get("output")
                            or raw_resp.get("text")
                            or raw_resp.get("content")
                            or str(raw_resp)
                        )
                    else:
                        resp_text = str(raw_resp)
                    resp_str = self._truncate(str(resp_text).strip())
                    blocks.append(f"**Tool Response (`{fr_name}`):**\n{resp_str}")

        return "\n\n".join(blocks)

    format_events_for_summary = _format_events_for_prompt

    async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
        """Summarize conversation events with scope filtering and fail-open resilience.

        Args:
            events: List of session events to consider for compaction summarization.

        Returns:
            Compaction event containing the generated summary, or None if skipped or on failure.
        """
        if not events:
            return None

        # 0. Single-turn idempotency: prevent running duplicate compaction within the exact same turn
        active_inv_id = _get_active_turn_invocation_id(events)
        if active_inv_id and active_inv_id == self._last_compacted_turn_inv_id:
            logger.debug(
                "Skipping duplicate compaction within the same invocation (%s).",
                active_inv_id,
            )
            return None

        # 1. Scope Filtering: Exclude scoped pipeline events (search_pipeline:{fc_id})
        # so compaction summarizes only high-level dialogue and clean tool returns,
        # without leaking internal ReAct retrieval loops or raw chunk payloads.
        unscoped_events = [
            e for e in events
            if getattr(e, "isolation_scope", None) is None
        ]
        if not unscoped_events:
            return None

        # Trim incomplete turns (e.g. trailing unanswered user questions or open tool calls)
        unscoped_events = _trim_to_completed_turns(unscoped_events)
        if not unscoped_events:
            return None

        with _start_compaction_span() as span:
            try:
                # Format prompt exclusively from unscoped high-level dialogue events
                conversation_history = self._format_events_for_prompt(unscoped_events)
                prompt = self._prompt_template.format(
                    conversation_history=conversation_history
                )

                model_name = getattr(self._llm, "model", "unknown")
                _safe_set(span, "openinference.span.kind", "LLM")
                _safe_set(span, "llm.model_name", model_name)
                _safe_set(span, "gen_ai.request.model", model_name)
                _safe_set(span, "input.value", prompt)
                _safe_set(span, "input.mime_type", "text/plain")
                _safe_set(span, "compaction.total_events", len(events))
                _safe_set(span, "compaction.unscoped_events", len(unscoped_events))

                llm_request = LlmRequest(
                    model=self._llm.model,
                    contents=[Content(role="user", parts=[Part(text=prompt)])],
                )
                summary_content = None
                summary_usage_metadata = None
                async for llm_response in self._llm.generate_content_async(
                    llm_request, stream=False
                ):
                    if llm_response.content:
                        summary_content = llm_response.content
                        summary_usage_metadata = llm_response.usage_metadata
                        break

                if summary_content is None:
                    _safe_set(span, "compaction.status", "empty_response")
                    return None

                summary_text = "".join(
                    p.text for p in summary_content.parts if getattr(p, "text", None)
                ).strip()
                _safe_set(span, "output.value", summary_text)
                _safe_set(span, "output.mime_type", "text/plain")

                if summary_usage_metadata:
                    prompt_tokens = getattr(summary_usage_metadata, "prompt_token_count", None)
                    completion_tokens = getattr(summary_usage_metadata, "candidates_token_count", None)
                    total_tokens = getattr(summary_usage_metadata, "total_token_count", None)
                    if prompt_tokens is not None:
                        _safe_set(span, "llm.token_count.prompt", prompt_tokens)
                        _safe_set(span, "gen_ai.usage.input_tokens", prompt_tokens)
                    if completion_tokens is not None:
                        _safe_set(span, "llm.token_count.completion", completion_tokens)
                        _safe_set(span, "gen_ai.usage.output_tokens", completion_tokens)
                    if total_tokens is not None:
                        _safe_set(span, "llm.token_count.total", total_tokens)
                        _safe_set(span, "gen_ai.usage.total_tokens", total_tokens)

                if _StatusCode and span is not None:
                    span.set_status(_StatusCode.OK)

                summary_content.role = "model"

                # The compaction range must cover exactly the span of events that were summarized.
                # Any trailing events excluded by _trim_to_completed_turns will have timestamp > end_timestamp
                # and will thus be safely preserved verbatim for the coordinator by ADK's _process_compaction_events.
                start_timestamp = getattr(unscoped_events[0], "timestamp", 0.0)
                end_timestamp = getattr(unscoped_events[-1], "timestamp", 0.0)

                compaction = EventCompaction(
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    compacted_content=summary_content,
                )

                actions = EventActions(compaction=compaction)

                if active_inv_id:
                    self._last_compacted_turn_inv_id = active_inv_id

                return Event(
                    author="user",
                    actions=actions,
                    invocation_id=Event.new_id(),
                    usage_metadata=summary_usage_metadata,
                )
            except Exception as exc:
                if span is not None:
                    _safe_set(span, "error.type", type(exc).__name__)
                    _safe_set(span, "error.message", str(exc))
                    if _StatusCode:
                        span.set_status(_StatusCode.ERROR, str(exc))
                logger.warning(
                    "Event compaction failed open; continuing turn without summary: %s",
                    exc,
                )
                return None


def _suppress_reasoning_effort(llm: Any) -> None:
    """Safely suppress reasoning/thinking effort on LLM instances without raising Pydantic field errors."""
    try:
        current_extra = getattr(llm, "extra_body", None)
        if isinstance(current_extra, dict):
            current_extra["reasoning"] = {"effort": "none"}
        else:
            current_extra = {"reasoning": {"effort": "none"}}
        try:
            llm.extra_body = current_extra
        except (ValueError, AttributeError):
            object.__setattr__(llm, "extra_body", current_extra)
    except Exception:
        pass

    try:
        if hasattr(llm, "_additional_args"):
            existing_extra = getattr(llm, "_additional_args", {}).get("extra_body") or {}
            if isinstance(existing_extra, dict):
                llm._additional_args["extra_body"] = {**existing_extra, "reasoning": {"effort": "none"}}
            else:
                llm._additional_args["extra_body"] = {"reasoning": {"effort": "none"}}
    except Exception:
        pass


def build_compaction_llm(
    cfg: dict[str, Any],
    zotero_data_dir: Path | str | None = None,
    default_model: Any = None,
) -> BaseLlm:
    """Build a fast, dedicated LiteLlm instance for context compaction with suppressed reasoning effort."""
    model_override = cfg.get("compaction_summarizer_model")
    if not model_override:
        if isinstance(default_model, dict):
            coord_val = default_model.get("coordinator") or default_model.get("coordinator_agent")
            if isinstance(coord_val, dict):
                model_override = coord_val.get("model")
            elif hasattr(coord_val, "model"):
                model_override = coord_val.model
            elif isinstance(coord_val, str):
                model_override = coord_val
        elif hasattr(default_model, "model"):
            model_override = default_model.model
        elif isinstance(default_model, str) and default_model:
            model_override = default_model

    if not model_override:
        model_override = DEFAULT_COMPACTION_SUMMARIZER_MODEL

    llm = build_agent_llm(
        {"model": model_override, "max_tokens": 1200, "temperature": 0.2},
        cfg,
        zotero_data_dir=zotero_data_dir,
    )
    # Suppress reasoning/thinking effort for compaction
    _suppress_reasoning_effort(llm)
    return llm


def build_events_compaction_config(
    cfg: dict[str, Any] | None = None,
    zotero_data_dir: Path | str | None = None,
    default_model: Any = None,
) -> EventsCompactionConfig | None:
    """Deactivated: native ADK pre-invocation triggers are replaced by post-turn adaptive compaction."""
    return None


def _extract_compacted_text(compacted_content: Any) -> str:
    """Extract clean summary text from compacted_content (Content, str, or dict)."""
    if not compacted_content:
        return ""
    if isinstance(compacted_content, str):
        return compacted_content.strip()
    parts = getattr(compacted_content, "parts", None)
    if parts is None and isinstance(compacted_content, dict):
        parts = compacted_content.get("parts")
    if parts:
        texts = []
        for p in parts:
            if isinstance(p, dict):
                t = p.get("text")
            else:
                t = getattr(p, "text", None)
            if t:
                texts.append(str(t).strip())
        return "\n".join(texts).strip()
    return str(compacted_content).strip()


def _get_completed_turn_groups(events: list[Event]) -> list[tuple[str, list[Event]]]:
    """Group unscoped, non-compaction events into chronological completed turns.

    Returns a list of (invocation_id, turn_events) pairs for turns that have an assistant completion.
    Incomplete turns (e.g. unanswered user queries or open tool calls) are excluded.
    """
    if not events:
        return []

    invocations: dict[str, list[Event]] = {}
    inv_order: list[str] = []
    synthetic_counter = 0
    current_inv = None

    for e in events:
        inv_id = e.invocation_id
        if not inv_id:
            if e.author == "user":
                synthetic_counter += 1
                current_inv = f"_synthetic_turn_{synthetic_counter}"
            inv_id = current_inv or f"_synthetic_turn_{synthetic_counter}"
        else:
            current_inv = inv_id

        if inv_id not in invocations:
            invocations[inv_id] = []
            inv_order.append(inv_id)
        invocations[inv_id].append(e)

    completed: list[tuple[str, list[Event]]] = []
    for inv_id in inv_order:
        inv_events = invocations[inv_id]
        has_assistant_completion = any(
            e.author != "user"
            and (
                (
                    e.content
                    and any(
                        p.text
                        for p in e.content.parts
                        if not getattr(p, "thought", False)
                    )
                )
                or any(
                    getattr(p, "function_response", None)
                    for p in (e.content.parts if e.content else [])
                )
            )
            for e in inv_events
        )
        if has_assistant_completion:
            completed.append((inv_id, inv_events))

    return completed


def _prepare_compaction_slice(
    unscoped_events: list[Event],
    live_events: list[Event],
    cfg: dict[str, Any],
    summarizer: Any,
) -> tuple[str, float, float, int, int] | None:
    """Prepares the conversation history prompt, timestamps, and metadata for adaptive compaction.

    Returns (prompt, start_timestamp, end_timestamp, slice_turns_count, k) or None.
    Fails open to None on any error without raising.
    """
    try:
        completed_turns = _get_completed_turn_groups(unscoped_events)
        n = len(completed_turns)

        k = max(
            0,
            int(
                cfg.get(
                    "adaptive_compaction_retention_turns",
                    DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
                )
            ),
        )

        if n <= k:
            logger.debug(
                "Adaptive compaction skipped: completed turns (%d) <= retention turns (%d).",
                n,
                k,
            )
            return None

        candidate_turns = completed_turns[: n - k]

        # Look for latest non-subsumed compaction event in live_events
        prev_compaction_event = None
        for e in reversed(live_events):
            actions = getattr(e, "actions", None)
            comp = getattr(actions, "compaction", None) if actions else None
            if comp is not None:
                prev_compaction_event = e
                break

        prev_comp = (
            getattr(getattr(prev_compaction_event, "actions", None), "compaction", None)
            if prev_compaction_event
            else None
        )
        prev_end_ts = getattr(prev_comp, "end_timestamp", None) if prev_comp else None

        if prev_comp is not None and prev_end_ts is not None:
            prev_end_ts_f = float(prev_end_ts)
            new_candidate_turns = []
            for inv_id, turn_evts in candidate_turns:
                turn_max_ts = max(
                    (getattr(e, "timestamp", 0.0) for e in turn_evts), default=0.0
                )
                if float(turn_max_ts) > prev_end_ts_f:
                    new_candidate_turns.append((inv_id, turn_evts))

            new_slice_events = [
                e
                for _, turn_evts in new_candidate_turns
                for e in turn_evts
                if float(getattr(e, "timestamp", 0.0)) > prev_end_ts_f
            ]

            if not new_slice_events:
                logger.debug(
                    "Adaptive compaction skipped: all candidate turns already subsumed by previous compaction."
                )
                return None

            previous_summary = _extract_compacted_text(prev_comp.compacted_content)
            formatted_new_events = summarizer._format_events_for_prompt(new_slice_events)
            conversation_history = (
                "### Existing Conversation Summary\n"
                f"{previous_summary}\n\n"
                "### New Completed Turns to Integrate\n"
                f"{formatted_new_events}"
            )
            prev_start_ts = getattr(prev_comp, "start_timestamp", None)
            if prev_start_ts is None:
                logger.debug(
                    "Previous compaction missing start_timestamp; skipping rolling compaction to prevent fragmentation."
                )
                return None
            start_timestamp = float(prev_start_ts)
            end_timestamp = float(getattr(new_slice_events[-1], "timestamp", 0.0))
            slice_turns_count = len(new_candidate_turns)
        else:
            slice_events = [e for _, turn_evts in candidate_turns for e in turn_evts]
            if not slice_events:
                return None
            conversation_history = summarizer._format_events_for_prompt(slice_events)
            start_timestamp = float(getattr(slice_events[0], "timestamp", 0.0))
            end_timestamp = float(getattr(slice_events[-1], "timestamp", 0.0))
            slice_turns_count = len(candidate_turns)

        template = getattr(
            summarizer, "_prompt_template", ZOCRATES_COMPACTION_PROMPT_TEMPLATE
        )
        prompt = template.format(conversation_history=conversation_history)
        return prompt, start_timestamp, end_timestamp, slice_turns_count, k
    except Exception as exc:
        logger.warning(
            "Adaptive context compaction slice preparation failed open: %s", exc
        )
        return None


async def compact_session_adaptive(
    session: Any,
    cfg: dict[str, Any] | None = None,
    summarizer: Any = None,
) -> Event | None:
    """Compact session history using adaptive retention slicing and rolling consolidation.

    - Slices history into candidate turns covering Turns 1 .. N-K, retaining the latest K turns verbatim.
    - If a previous compaction event exists:
      Consolidates the previous summary with new completed turns up to Turn N-K,
      preserving start_timestamp = previous_compaction.start_timestamp and setting
      end_timestamp = new_slice_events[-1].timestamp.
    - If no previous compaction event exists:
      Summarizes all events from Turns 1 .. N-K spanning [slice_events[0].ts, slice_events[-1].ts].
    - Wraps LLM invocation in OpenTelemetry span `compact_session_history` (kind=LLM) with full attributes.
    - Fails open to None on any error or timeout without raising.
    """
    if cfg is None:
        try:
            cfg = load_zocrates_config()
        except Exception as exc:
            logger.debug("Failed to load zocrates config: %s", exc)
            cfg = {}

    if summarizer is None:
        try:
            from zocrates.agents.app import get_compaction_summarizer

            summarizer = get_compaction_summarizer()
        except Exception as exc:
            logger.debug("Failed to get compaction summarizer: %s", exc)
            return None

    if summarizer is None:
        return None

    llm = getattr(summarizer, "_llm", getattr(summarizer, "llm", None))
    if llm is None:
        return None

    raw_events = (
        getattr(session, "events", [])
        if hasattr(session, "events")
        else (session if isinstance(session, list) else [])
    )
    live_events = _safe_apply_rewinds(raw_events)

    # Exclude pipeline isolation scopes and existing compaction events
    unscoped_events = [
        e
        for e in live_events
        if getattr(e, "isolation_scope", None) is None
        and not _is_compaction_event(e)
    ]

    prep_result = _prepare_compaction_slice(
        unscoped_events, live_events, cfg, summarizer
    )
    if prep_result is None:
        return None

    prompt, start_timestamp, end_timestamp, slice_turns_count, k = prep_result

    # Suppress reasoning effort
    _suppress_reasoning_effort(llm)

    model_name = getattr(llm, "model", None) or cfg.get(
        "compaction_summarizer_model", DEFAULT_COMPACTION_SUMMARIZER_MODEL
    )

    with _start_compaction_span("compact_session_history") as span:
        _safe_set(span, "openinference.span.kind", "LLM")
        _safe_set(span, "llm.model_name", str(model_name))
        _safe_set(span, "gen_ai.request.model", str(model_name))
        _safe_set(
            span,
            "llm.invocation_parameters",
            {"temperature": 0.2, "reasoning": {"effort": "none"}},
        )
        _safe_set(span, "input.value", prompt)
        _safe_set(span, "input.mime_type", "text/plain")
        _safe_set(span, "compaction.summarize.start_timestamp", start_timestamp)
        _safe_set(span, "compaction.summarize.end_timestamp", end_timestamp)
        _safe_set(span, "compaction.summarize.slice_turns", slice_turns_count)
        _safe_set(span, "compaction.summarize.retained_turns", k)

        t0 = time.perf_counter()
        try:
            llm_request = LlmRequest(
                model=getattr(llm, "model", None),
                contents=[Content(role="user", parts=[Part(text=prompt)])],
            )
            summary_content = None
            summary_usage_metadata = None
            async for llm_response in llm.generate_content_async(
                llm_request, stream=False
            ):
                if llm_response.content:
                    summary_content = llm_response.content
                    summary_usage_metadata = llm_response.usage_metadata
                    break

            latency_ms = (time.perf_counter() - t0) * 1000.0
            _safe_set(span, "compaction.summarize.latency_ms", round(latency_ms, 2))

            if summary_content is None or not summary_content.parts:
                _safe_set(span, "compaction.status", "empty_response")
                return None

            summary_text = "".join(
                p.text for p in summary_content.parts if getattr(p, "text", None)
            ).strip()

            if not summary_text:
                _safe_set(span, "compaction.status", "empty_text")
                return None

            _safe_set(span, "output.value", summary_text)
            _safe_set(span, "output.mime_type", "text/plain")

            if summary_usage_metadata:
                pt = getattr(summary_usage_metadata, "prompt_token_count", None)
                ct = getattr(summary_usage_metadata, "candidates_token_count", None)
                tt = getattr(summary_usage_metadata, "total_token_count", None)
                if pt is not None:
                    _safe_set(span, "llm.token_count.prompt", pt)
                    _safe_set(span, "gen_ai.usage.input_tokens", pt)
                if ct is not None:
                    _safe_set(span, "llm.token_count.completion", ct)
                    _safe_set(span, "gen_ai.usage.output_tokens", ct)
                if tt is not None:
                    _safe_set(span, "llm.token_count.total", tt)
                    _safe_set(span, "gen_ai.usage.total_tokens", tt)

            if _StatusCode is not None and span is not None:
                span.set_status(_StatusCode.OK)

            clean_parts = [
                Part.from_text(text=p.text)
                for p in summary_content.parts
                if getattr(p, "text", None) and not getattr(p, "thought", False)
            ]
            if not clean_parts:
                clean_parts = [Part.from_text(text=summary_text)]

            compacted_content = Content(role="model", parts=clean_parts)
            compaction = EventCompaction(
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                compacted_content=compacted_content,
            )
            actions = EventActions(compaction=compaction)

            return Event(
                author="user",
                actions=actions,
                invocation_id=Event.new_id(),
                usage_metadata=summary_usage_metadata,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            _safe_set(span, "compaction.summarize.latency_ms", round(latency_ms, 2))
            _safe_set(span, "error.type", type(exc).__name__)
            _safe_set(span, "error.message", str(exc))
            if _StatusCode is not None and span is not None:
                span.set_status(_StatusCode.ERROR, str(exc))
            logger.warning("Adaptive context compaction LLM call failed open: %s", exc)
            return None


async def run_adaptive_post_invocation_compaction(
    runner: Any,
    session: Any,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Execute post-turn adaptive compaction check and compaction event append.

    Returns True if a compaction event was generated and appended to the session, False otherwise.
    Fails open defensively without raising exceptions.
    """
    if cfg is None:
        try:
            cfg = load_zocrates_config()
        except Exception as exc:
            logger.debug("Failed to load zocrates config: %s", exc)
            cfg = {}

    if not cfg.get("enable_adaptive_compaction", DEFAULT_ENABLE_ADAPTIVE_COMPACTION):
        return False

    if not session or not getattr(session, "events", None):
        return False

    last_ev = session.events[-1]
    # Check HITL / paused state: open function_call
    if last_ev.content and any(
        getattr(p, "function_call", None) for p in (last_ev.content.parts or [])
    ):
        logger.debug(
            "Skipping adaptive compaction: invocation interrupted mid-turn for HITL / open function call."
        )
        return False

    # Requested tool confirmation or auth config
    if getattr(last_ev, "actions", None) and (
        getattr(last_ev.actions, "requested_tool_confirmations", None)
        or getattr(last_ev.actions, "requested_auth_configs", None)
    ):
        logger.debug(
            "Skipping adaptive compaction: invocation awaiting tool confirmation."
        )
        return False

    try:
        should_compress, reason = await asyncio.to_thread(
            should_trigger_adaptive_compaction, session, cfg
        )
    except Exception as exc:
        logger.warning(
            "Error evaluating should_trigger_adaptive_compaction: %s. Failing open.",
            exc,
        )
        return False

    logger.debug(
        "Adaptive compaction decision check: %s (%s)", should_compress, reason
    )
    if not should_compress:
        return False

    try:
        compaction_event = await compact_session_adaptive(session, cfg)
    except Exception as exc:
        logger.warning(
            "Error generating adaptive compaction event: %s. Failing open.", exc
        )
        return False

    if compaction_event is None:
        return False

    session_service = getattr(runner, "session_service", None)
    if not session_service:
        logger.warning("Runner has no session_service; cannot append compaction event.")
        return False

    try:
        await session_service.append_event(session=session, event=compaction_event)
        logger.info(
            "Successfully appended adaptive compaction event to session %s",
            getattr(session, "id", getattr(session, "session_id", "unknown")),
        )
        return True
    except Exception as exc:
        logger.warning(
            "Defensively caught exception appending compaction event (e.g. StaleSessionError): %s",
            exc,
        )
        return False


__all__ = [
    "ZOCRATES_COMPACTION_PROMPT_TEMPLATE",
    "FailOpenLlmEventSummarizer",
    "build_compaction_llm",
    "build_events_compaction_config",
    "compact_session_adaptive",
    "run_adaptive_post_invocation_compaction",
]
