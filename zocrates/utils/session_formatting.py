"""Session formatting and token aggregation utilities for Zocrates.

Handles conversion of ADK Session and Event objects into client-facing
JSON structures, aggregating multi-agent turns, normalizing token metrics,
and mapping internal agent names to user-facing titles.
"""

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from google.adk.events._rewind_events import _apply_rewinds

from zocrates.utils.cost_calculator import aggregate_turn_cost


def parse_usage_metadata(usage_meta: Any) -> dict[str, int] | None:
    """Extract token counts into a normalized dictionary.

    Handles attribute-based ADK objects and plain dictionaries.
    Returns None if all primary token counts are absent.
    """
    if not usage_meta:
        return None

    prompt_tokens = getattr(usage_meta, "prompt_token_count", None)
    candidates_tokens = getattr(usage_meta, "candidates_token_count", None)
    thoughts_tokens = getattr(usage_meta, "thoughts_token_count", None)
    total_tokens = getattr(usage_meta, "total_token_count", None)
    cached_tokens = getattr(usage_meta, "cached_content_token_count", None)

    if prompt_tokens is None and isinstance(usage_meta, dict):
        prompt_tokens = usage_meta.get("prompt_token_count") or usage_meta.get("prompt_tokens")
        candidates_tokens = usage_meta.get("candidates_token_count") or usage_meta.get("candidates_tokens")
        thoughts_tokens = usage_meta.get("thoughts_token_count") or usage_meta.get("thoughts_tokens")
        total_tokens = usage_meta.get("total_token_count") or usage_meta.get("total_tokens")
        cached_tokens = usage_meta.get("cached_content_token_count") or usage_meta.get("cached_tokens")

    if all(x is None for x in (prompt_tokens, candidates_tokens, total_tokens)):
        return None

    p = int(prompt_tokens or 0)
    c = int(candidates_tokens or 0)
    tot = int(total_tokens or 0)
    if tot == 0 and (p > 0 or c > 0):
        tot = p + c

    return {
        "prompt_tokens": p,
        "candidates_tokens": c,
        "thoughts_tokens": int(thoughts_tokens or 0),
        "cached_tokens": int(cached_tokens or 0),
        "total_tokens": tot,
    }


def parse_finish_reason(fr: Any) -> str | None:
    """Extract finish reason as a clean string."""
    if not fr:
        return None
    if hasattr(fr, "name"):
        return str(fr.name)
    s = str(fr)
    if "." in s:
        return s.split(".")[-1]
    return s


def format_author_name(raw_author: str | None) -> str:
    """Format internal agent names into customer-facing names without subagent references."""
    if not raw_author or raw_author.lower() in ("user", "human"):
        return "User"
    s = raw_author.strip()
    if s.startswith("Zocrates/"):
        s = s[9:].strip()

    s = re.sub(r"sub[-_]?agent", "agent", s, flags=re.IGNORECASE)
    normalized = re.sub(r"[-_]", " ", s).lower().strip()
    if normalized in (
        "library synthesizer agent",
        "library synthesizer",
        "synthesizer agent",
        "synthesizer",
        "synthesis agent",
    ):
        return "Library Synthesis"
    if normalized in (
        "library search agent",
        "library search",
        "search agent",
        "search",
        "search subagent",
    ):
        return "Library Search"
    if normalized in ("online search agent", "online search", "discovery search agent"):
        return "Online Discovery"
    if normalized in ("online synthesizer agent", "online synthesizer", "discovery synthesizer agent"):
        return "Discovery Synthesis"
    if normalized in (
        "coordinator agent", "coordinator", "dialogue agent", "dialogue", "dialogue coordinator",
        "zocrates mode router", "zocrates mode router node", "mode router", "mode router node", "router",
    ):
        return "Zocrates"

    words = [w.capitalize() for w in re.split(r"[-_]+", s) if w]
    res = " ".join(words)
    res = re.sub(r"\bSubagent\b", "Agent", res, flags=re.IGNORECASE)
    return res or "Zocrates"


def extract_event_parts(evt: Any) -> tuple[str, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract text, thoughts, function calls, and responses from an event."""
    content = getattr(evt, "content", None)
    parts = getattr(content, "parts", None) if content else None
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    if isinstance(content, str) and content.strip():
        texts.append(content.strip())
    elif parts:
        current_thought_chunk: list[str] = []
        for p in parts:
            thought_val = getattr(p, "thought", None)
            if bool(thought_val):
                if isinstance(thought_val, str) and thought_val:
                    current_thought_chunk.append(thought_val)
                elif hasattr(p, "text") and p.text and not isinstance(p.text, MagicMock):
                    current_thought_chunk.append(str(p.text))
            else:
                if current_thought_chunk:
                    joined_th = "".join(current_thought_chunk).strip()
                    if joined_th:
                        thoughts.append(joined_th)
                    current_thought_chunk = []

                fc = getattr(p, "function_call", None)
                if fc is not None and not isinstance(fc, MagicMock) and getattr(fc, "name", None):
                    calls.append({
                        "name": fc.name,
                        "args": getattr(fc, "args", {}) or {},
                        "id": getattr(fc, "id", None),
                    })
                fr = getattr(p, "function_response", None)
                if fr is not None and not isinstance(fr, MagicMock) and getattr(fr, "name", None):
                    responses.append({
                        "name": fr.name,
                        "response": getattr(fr, "response", None),
                        "id": getattr(fr, "id", None),
                    })
                if hasattr(p, "text") and p.text and not isinstance(p.text, MagicMock) and str(p.text).strip():
                    texts.append(str(p.text).strip())

        if current_thought_chunk:
            joined_th = "".join(current_thought_chunk).strip()
            if joined_th:
                thoughts.append(joined_th)

    return "\n".join(texts), thoughts, calls, responses


def _is_compaction_event(e: Any) -> bool:
    """Return True if event is an ADK compaction event, ignoring unconfigured test mocks."""
    actions = getattr(e, "actions", None)
    if actions is None:
        return False
    if isinstance(actions, MagicMock) and "compaction" not in actions.__dict__:
        return False
    compaction = getattr(actions, "compaction", None)
    if compaction is None:
        return False
    if (
        isinstance(compaction, MagicMock)
        and "compacted_content" not in compaction.__dict__
        and "compaction" not in getattr(actions, "__dict__", {})
    ):
        return False
    return bool(compaction)


def format_session_for_ui(
    session: Any,
    default_model: str | None = None,
    zotero_data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Format an ADK Session object into the UI JSON schema expected by sidebar.js.

    Groups events by conversational turn (starting with each `user` event),
    accumulates all intermediate thoughts, tool calls, and returns into
    `intermediate_steps`, aggregates token usage across all steps of the turn,
    computes multi-agent turn costs in USD and agent model mappings,
    and emits exactly two messages per turn (user + assistant).

    Args:
        session: An ADK ``Session`` object with ``id``, ``state``, and
            ``events`` attributes.
        default_model: Optional default model name fallback.
        zotero_data_dir: Optional Zotero data directory for catalog discovery.

    Returns:
        Dict with keys ``id``, ``title``, ``created_at``, ``updated_at``,
        ``total_cost_usd``, ``total_tokens``, ``total_turns``,
        ``session_metadata``, and ``messages``.
    """
    events = getattr(session, "events", []) or []
    has_rewinds = any(
        isinstance(getattr(getattr(e, "actions", None), "rewind_before_invocation_id", None), str)
        for e in events
    )
    if has_rewinds:
        events = _apply_rewinds(events)
    events = [e for e in events if not _is_compaction_event(e)]
    messages: list[dict[str, Any]] = []

    turns: list[tuple[Any | None, list[Any]]] = []
    current_user_evt = None
    current_asst_evts: list[Any] = []
    leading_asst_evts: list[Any] = []
    session_total_cost = 0.0
    session_total_tokens = 0

    for evt in events:
        author = getattr(evt, "author", "assistant")
        is_user = (author == "user")

        if is_user:
            u_txt, _, _, u_frs = extract_event_parts(evt)
            if not u_txt.strip() and u_frs:
                # FR-only user event (e.g. resume FunctionResponse).
                # Fold into assistant events to avoid creating an empty user bubble or splitting turn.
                if current_user_evt is not None:
                    current_asst_evts.append(evt)
                else:
                    leading_asst_evts.append(evt)
                continue

            if current_user_evt is not None:
                turns.append((current_user_evt, current_asst_evts))
            current_user_evt = evt
            current_asst_evts = []
        else:
            if current_user_evt is not None:
                current_asst_evts.append(evt)
            else:
                leading_asst_evts.append(evt)

    if current_user_evt is not None:
        turns.append((current_user_evt, current_asst_evts))

    # If there are no user turns at all, treat leading assistant events as turns
    if not turns and leading_asst_evts:
        turns.append((None, leading_asst_evts))
    elif leading_asst_evts:
        for evt in leading_asst_evts:
            txt, ths, _, _ = extract_event_parts(evt)
            if txt or ths:
                evt_cost, evt_models = aggregate_turn_cost(
                    [evt],
                    default_model=default_model,
                    zotero_data_dir=zotero_data_dir,
                )
                session_total_cost += evt_cost
                lead_u = parse_usage_metadata(getattr(evt, "usage_metadata", None))
                if lead_u:
                    lead_u["cost_usd"] = round(evt_cost, 6)
                    lead_u["agent_models"] = evt_models
                    session_total_tokens += lead_u.get("total_tokens", 0)
                messages.append({
                    "id": getattr(evt, "id", None),
                    "role": "assistant",
                    "text": txt,
                    "thought": "\n\n".join(ths) if ths else None,
                    "author": format_author_name(getattr(evt, "author", None)),
                    "timestamp": getattr(evt, "timestamp", None),
                    "model": getattr(evt, "model_version", None),
                    "usage": lead_u,
                    "turn_cost_usd": round(evt_cost, 6),
                    "agent_models": evt_models,
                    "intermediate_steps": [],
                })

    for user_evt, asst_evts in turns:
        context_items = None
        snippets = None
        if user_evt is not None:
            user_text, _, _, _ = extract_event_parts(user_evt)
            user_ts = getattr(user_evt, "timestamp", None)
            actions = getattr(user_evt, "actions", None)
            state_delta = getattr(actions, "state_delta", {}) if actions else {}
            context_items = state_delta.get("active_context_items") if isinstance(state_delta, dict) else None
            snippets = state_delta.get("snippets") if isinstance(state_delta, dict) else None

            messages.append({
                "id": getattr(user_evt, "id", None),
                "role": "user",
                "text": user_text,
                "author": "User",
                "timestamp": user_ts,
                "active_context_items": context_items,
                "snippets": snippets,
                "invocation_id": getattr(user_evt, "invocation_id", None),
            })

        if not asst_evts:
            continue

        # Aggregate token usage across all assistant events in the turn
        turn_prompt = 0
        turn_candidates = 0
        turn_thoughts = 0
        turn_cached = 0
        turn_total = 0
        has_usage = False

        for evt in asst_evts:
            u = parse_usage_metadata(getattr(evt, "usage_metadata", None))
            if u:
                has_usage = True
                turn_prompt += u.get("prompt_tokens", 0)
                turn_candidates += u.get("candidates_tokens", 0)
                turn_thoughts += u.get("thoughts_tokens", 0)
                turn_cached += u.get("cached_tokens", 0)
                turn_total += u.get("total_tokens", 0)

        aggregated_usage = {
            "prompt_tokens": turn_prompt,
            "candidates_tokens": turn_candidates,
            "thoughts_tokens": turn_thoughts,
            "cached_tokens": turn_cached,
            "total_tokens": turn_total,
        } if has_usage else None

        # Determine the final response event and text
        text_events: list[tuple[Any, str]] = []
        for evt in asst_evts:
            txt, _, _, _ = extract_event_parts(evt)
            if txt:
                text_events.append((evt, txt))

        final_evt = None
        final_text = ""
        SYNTHESIZER_AUTHORS = (
            "library_synthesizer_agent",
            "library_synthesizer",
            "online_synthesizer_agent",
            "online_synthesizer",
            "synthesizer_agent",
            "synthesizer",
        )
        for evt, txt in text_events:
            auth = getattr(evt, "author", "") or ""
            if auth in SYNTHESIZER_AUTHORS:
                final_evt = evt
                final_text = txt
                break

        if not final_evt and text_events:
            final_evt, final_text = text_events[-1]

        ev_meta = getattr(final_evt, "custom_metadata", None) or {}
        if not isinstance(ev_meta, dict):
            ev_meta = {}
        citations = ev_meta.get("citations")
        references = ev_meta.get("references")
        candidates = ev_meta.get("candidates")
        citation_verification = ev_meta.get("citation_verification")
        enriched_text = ev_meta.get("enriched_text")

        if getattr(session, "state", None):
            audit_history = session.state.get("retrieved_evidence") or []
            inv_id = getattr(final_evt or asst_evts[-1], "invocation_id", None)
            for entry in reversed(audit_history):
                # Strictly scope to matching invocation_id to prevent leaking across turns
                entry_inv = entry.get("invocation_id")
                if (inv_id and entry_inv == inv_id) or (not inv_id and not entry_inv):
                    if not citations:
                        citations = entry.get("citations")
                    if not references:
                        references = entry.get("references")
                    if not enriched_text:
                        enriched_text = entry.get("enriched_text")
                    if not citation_verification:
                        citation_verification = entry.get("citation_verification_telemetry")
                    break

        if enriched_text:
            final_text = enriched_text

        if not candidates and getattr(session, "state", None):
            inv_id = getattr(final_evt or asst_evts[-1], "invocation_id", None)
            cand_inv_id = session.state.get("candidates_invocation_id")
            if inv_id and cand_inv_id and inv_id == cand_inv_id:
                candidates = session.state.get("candidates") or session.state.get("selected_candidates")

        # Compute multi-agent turn cost and model mapping
        turn_cost, agent_models = aggregate_turn_cost(
            asst_evts,
            default_model=default_model,
            zotero_data_dir=zotero_data_dir,
        )

        # Inspect citation_verification assessments for sentence attribution accounting
        if citation_verification and isinstance(citation_verification, dict):
            sa_cost = 0.0
            sa_prompt = 0
            sa_completion = 0
            sa_total = 0
            has_sa = False
            for assessment in citation_verification.get("assessments", []):
                if not isinstance(assessment, dict):
                    continue
                sa = assessment.get("sentence_attribution")
                if sa and isinstance(sa, dict) and sa.get("status") == "ok":
                    has_sa = True
                    try:
                        sa_cost += float(sa.get("cost_usd") or 0.0)
                    except (ValueError, TypeError):
                        pass
                    u = sa.get("usage") or {}
                    if isinstance(u, dict):
                        try:
                            p = int(u.get("prompt_tokens") or 0)
                        except (ValueError, TypeError):
                            p = 0
                        try:
                            c = int(u.get("completion_tokens") or 0)
                        except (ValueError, TypeError):
                            c = 0
                        try:
                            t = int(u.get("total_tokens") or (p + c))
                        except (ValueError, TypeError):
                            t = p + c
                        sa_prompt += p
                        sa_completion += c
                        sa_total += t

            if has_sa:
                turn_cost += sa_cost
                if aggregated_usage is None:
                    aggregated_usage = {
                        "prompt_tokens": sa_prompt,
                        "candidates_tokens": sa_completion,
                        "thoughts_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": sa_total,
                    }
                else:
                    aggregated_usage["prompt_tokens"] = aggregated_usage.get("prompt_tokens", 0) + sa_prompt
                    aggregated_usage["candidates_tokens"] = aggregated_usage.get("candidates_tokens", 0) + sa_completion
                    aggregated_usage["total_tokens"] = aggregated_usage.get("total_tokens", 0) + sa_total

        session_total_cost += turn_cost
        if aggregated_usage:
            session_total_tokens += aggregated_usage.get("total_tokens", 0)

        if aggregated_usage is not None:
            aggregated_usage["cost_usd"] = round(turn_cost, 6)
            aggregated_usage["agent_models"] = agent_models
        elif agent_models:
            aggregated_usage = {
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "thoughts_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
                "cost_usd": round(turn_cost, 6),
                "agent_models": agent_models,
            }

        # Extract intermediate steps
        intermediate_steps: list[dict[str, Any]] = []
        all_thoughts: list[str] = []
        for evt in asst_evts:
            auth = format_author_name(getattr(evt, "author", None))
            ts = getattr(evt, "timestamp", None)
            txt, thoughts, calls, responses = extract_event_parts(evt)

            for th in thoughts:
                all_thoughts.append(th)
                intermediate_steps.append({
                    "type": "thought",
                    "author": auth,
                    "content": th,
                    "timestamp": ts,
                })

            if txt and evt != final_evt:
                intermediate_steps.append({
                    "type": "agent_text",
                    "author": auth,
                    "text": txt,
                    "timestamp": ts,
                })

            for call in calls:
                if call.get("name") != "finish_task":
                    intermediate_steps.append({
                        "type": "tool_call",
                        "author": auth,
                        "name": call.get("name"),
                        "args": call.get("args"),
                        "call_id": call.get("id"),
                        "timestamp": ts,
                    })

            for resp in responses:
                if resp.get("name") != "finish_task":
                    intermediate_steps.append({
                        "type": "tool_return",
                        "name": resp.get("name"),
                        "response": resp.get("response"),
                        "call_id": resp.get("id"),
                        "timestamp": ts,
                    })

        # Final metadata
        final_author = format_author_name(getattr(final_evt, "author", None)) if final_evt else "Zocrates"
        final_model = getattr(final_evt, "model_version", None) if final_evt else None
        if not final_model:
            for evt in reversed(asst_evts):
                if getattr(evt, "model_version", None):
                    final_model = evt.model_version
                    break

        final_finish = getattr(final_evt, "finish_reason", None) if final_evt else None
        if not final_finish:
            for evt in reversed(asst_evts):
                if getattr(evt, "finish_reason", None):
                    final_finish = evt.finish_reason
                    break
        final_finish = parse_finish_reason(final_finish)

        final_ts = getattr(final_evt or asst_evts[-1], "timestamp", None)

        # Duration
        duration_sec = None
        try:
            start_ts = getattr(user_evt, "timestamp", None) if user_evt else None
            end_ts = final_ts or getattr(asst_evts[-1], "timestamp", None)
            if start_ts is not None and end_ts is not None:
                duration_sec = round(float(end_ts) - float(start_ts), 2)
                if duration_sec < 0:
                    duration_sec = None
        except Exception:
            pass



        messages.append({
            "id": getattr(final_evt or asst_evts[-1], "id", None),
            "role": "assistant",
            "text": final_text,
            "thought": "\n\n".join(all_thoughts) if all_thoughts else None,
            "author": final_author,
            "model": final_model,
            "intermediate_steps": intermediate_steps,
            "usage": aggregated_usage,
            "turn_cost_usd": round(turn_cost, 6),
            "agent_models": agent_models,
            "finish_reason": final_finish,
            "timestamp": final_ts,
            "duration": duration_sec,
            "active_context_items": context_items,
            "invocation_id": getattr(final_evt or asst_evts[-1], "invocation_id", None),
            "citations": citations,
            "references": references,
            "candidates": candidates,
            "citation_verification": citation_verification,
        })

    title = session.state.get("session_title", "New Chat") if getattr(session, "state", None) else "New Chat"
    if not title or title == "New Chat":
        if messages and messages[0].get("text"):
            first_text = messages[0]["text"].strip()
            title = first_text[:24] + ("..." if len(first_text) > 24 else "")

    # Check for unresolved HITL interrupts
    pending_hitl = None
    unresolved_interrupts: set[str] = set()
    latest_hitl_event = None
    latest_hitl_fc = None

    for ev in events:
        if getattr(ev, "long_running_tool_ids", None):
            unresolved_interrupts.update(ev.long_running_tool_ids)
            if ev.content and ev.content.parts:
                for p in ev.content.parts:
                    fc = getattr(p, "function_call", None)
                    if fc and getattr(fc, "name", None) == "adk_request_input" and getattr(fc, "id", None) in unresolved_interrupts:
                        latest_hitl_event = ev
                        latest_hitl_fc = fc
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                fr = getattr(p, "function_response", None)
                if fr and getattr(fr, "id", None) in unresolved_interrupts:
                    unresolved_interrupts.discard(fr.id)
                    if latest_hitl_fc and getattr(latest_hitl_fc, "id", None) == fr.id:
                        latest_hitl_event = None
                        latest_hitl_fc = None

    if unresolved_interrupts and latest_hitl_fc is not None:
        fc_args = getattr(latest_hitl_fc, "args", {}) or {}
        if not isinstance(fc_args, dict):
            fc_args = {}
        pending_hitl = {
            "interrupt_id": latest_hitl_fc.id,
            "invocation_id": getattr(latest_hitl_event, "invocation_id", None),
            "message": fc_args.get("message"),
            "payload": fc_args.get("payload", {}),
        }
        if messages:
            messages[-1]["pending_hitl"] = pending_hitl

    valid_turns_count = sum(
        1 for m in messages
        if m.get("role") == "assistant"
        and m.get("text")
        and not (m.get("text") or "").startswith("Hello! I am Zocrates")
    )

    res = {
        "id": session.id,
        "title": title,
        "created_at": getattr(session, "created_at", None),
        "updated_at": getattr(session, "updated_at", None),
        "total_cost_usd": round(session_total_cost, 6),
        "total_tokens": session_total_tokens,
        "total_turns": valid_turns_count,
        "session_metadata": {
            "total_cost_usd": round(session_total_cost, 6),
            "total_tokens": session_total_tokens,
            "total_turns": valid_turns_count,
        },
        "messages": messages,
    }
    if pending_hitl:
        res["pending_hitl"] = pending_hitl
    return res
