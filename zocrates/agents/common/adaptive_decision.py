"""Post-turn decision engine core for Google ADK 2.8+ adaptive context compaction.

Evaluates multi-turn dialogue state against a probabilistic decision model rubric
to determine if earlier turns should be compacted into a concise summary while
retaining the most recent turns verbatim.
"""

from collections.abc import Iterator
import contextlib
from dataclasses import dataclass
import json
import logging
import math
import time
from typing import Any
import urllib.parse

import requests

from zocrates.config import (
    DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL,
    DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS,
    DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
    DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD,
    DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING,
    DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR,
    DEFAULT_ENABLE_ADAPTIVE_COMPACTION,
    get_secure_api_key,
)
from zocrates.agents.common.state_keys import RESEARCH_TURN_HISTORY
from zocrates.utils.session_formatting import extract_event_parts

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode
except Exception:
    _otel_trace = None
    _StatusCode = None

try:
    from google.adk.events._rewind_events import _apply_rewinds
except ImportError:
    _apply_rewinds = lambda evts: evts  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "DECISION_PROMPT_INSTRUCTIONS",
    "CompletedTurn",
    "count_completed_turns_since_last_compaction",
    "evaluate_adaptive_compaction_rubric",
    "extract_decision_state",
    "get_completed_turns",
    "get_coordinator_prompt_tokens",
    "is_local_model",
    "probe_local_laya_health",
    "should_trigger_adaptive_compaction",
    "_is_compaction_event",
    "_safe_apply_rewinds",
]

# Service endpoints & defaults
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8005/v1/systemone"
DEFAULT_LOCAL_HEALTH_URL = "http://127.0.0.1:8005/health"
DEFAULT_LOCAL_HEALTH_TIMEOUT_SEC: float = 0.150  # 150ms probe
DEFAULT_TIMEOUT_SEC: float = 10.0
DEFAULT_RETRY_BACKOFF_SEC: float = 0.250  # 250ms backoff on HTTP 429

# Exact prompt rubric and decision rule for compaction warranted probe
DECISION_PROMPT_INSTRUCTIONS = (
    "Task: Decide whether `earlier_discussion` should be compacted into a concise summary to preserve context space for upcoming turns.\n\n"
    "Rubric:\n"
    "- CLOSED-UNIT: Did `earlier_discussion` conclude a complete sub-topic or sub-analysis?\n"
    "- PROGRESS/PIVOT: In the latter part of `earlier_discussion` or in `latest_turn`, has the user pivoted to a different research topic or advanced to a new phase?\n"
    "- NOT STUCK: Is the conversation free from an unresolved clarification or correction loop?\n\n"
    "Decision Rule:\n"
    "- Compaction is WARRANTED if and only if ALL criteria are satisfied: earlier turns form a resolved unit, the conversation is advancing to new ground, and there is no active confusion or clarification loop.\n"
    "- Compaction is NOT WARRANTED if the user is still actively drilling down, clarifying, or troubleshooting the exact same point, or if the conversation is mid-thought.\n\n"
    "Determine if compacting `earlier_discussion` is warranted."
)


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
def _decision_span(name: str = "compaction_decision_probe") -> Iterator[Any]:
    """Context manager yielding an active OpenTelemetry span or None on fallback.

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
        tracer = _otel_trace.get_tracer("zocrates.adaptive_compaction")
        span_cm = tracer.start_as_current_span(name)
    except Exception as exc:
        logger.debug("Failed starting decision probe span: %s", exc)
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


def is_local_model(model_name: str) -> bool:
    """Check if model name indicates local Laya provider dispatch."""
    cleaned = (model_name or "").strip().lower()
    return (
        cleaned in ("laya", "local", "local_laya", "local/laya")
        or cleaned.startswith("local:")
        or cleaned.startswith("local/")
    )


def probe_local_laya_health(
    health_url: str = DEFAULT_LOCAL_HEALTH_URL,
    timeout: float = DEFAULT_LOCAL_HEALTH_TIMEOUT_SEC,
) -> bool:
    """Fast health check probe against local Laya server."""
    urls = [health_url]
    if health_url.endswith("/health"):
        urls.append(health_url + "z")
    elif health_url.endswith("/healthz"):
        urls.append(health_url[:-1])

    for url in urls:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                return True
        except Exception:
            continue
    return False


def _is_compaction_event(e: Any) -> bool:
    """Check if event is an ADK compaction event."""
    actions = getattr(e, "actions", None)
    if actions is None:
        return False
    return getattr(actions, "compaction", None) is not None


def _safe_apply_rewinds(events: list[Any]) -> list[Any]:
    """Safely apply rewinds to events list, failing open to original list if unneeded."""
    if not events:
        return []
    has_rewinds = any(
        isinstance(
            getattr(getattr(e, "actions", None), "rewind_before_invocation_id", None),
            str,
        )
        for e in events
    )
    if has_rewinds:
        try:
            return _apply_rewinds(events)
        except Exception as exc:
            logger.debug("Failed applying rewinds: %s", exc)
            return list(events)
    return list(events)


def _format_turn(user_query: str, assistant_response: str) -> str:
    """Format a single turn into clean serial Markdown without headers or bullet points."""
    return f"**User**: {user_query.strip()}\n**Assistant**: {assistant_response.strip()}"


def get_coordinator_prompt_tokens(events: list[Any]) -> int | None:
    """Extract prompt token count strictly from the coordinator agent's latest unscoped event.

    Filters out subagent research pipeline events (where isolation_scope is not None)
    and returns prompt_token_count from usage_metadata of the most recent unscoped event.
    """
    if not events:
        return None
    for event in reversed(events):
        if getattr(event, "isolation_scope", None) is not None:
            continue
        meta = getattr(event, "usage_metadata", None)
        if meta is None:
            continue
        pt = getattr(meta, "prompt_token_count", None)
        if pt is None and isinstance(meta, dict):
            pt = meta.get("prompt_token_count") or meta.get("prompt_tokens")
        if pt is not None:
            try:
                return int(pt)
            except (ValueError, TypeError):
                pass
    return None


@dataclass(frozen=True)
class CompletedTurn:
    """Represents a completed dialogue turn with user query and assistant response."""

    user_query: str
    assistant_response: str
    invocation_id: str | None = None


def _extract_completed_turns_from_events(
    events: list[Any],
    research_history: list[dict[str, Any]] | None = None,
) -> list[CompletedTurn]:
    """Group unscoped events into completed turns, extracting user queries and assistant responses.

    - For research pipeline turns: extracts concise findings summary (<150 words) from
      `conclude_output` in `RESEARCH_TURN_HISTORY` matched by `invocation_id`.
      Raw chunk IDs, full syntheses, and bibliographies are strictly excluded.
    - For conversational turns: extracts coordinator direct text untruncated (thoughts excluded).
    - Zero tool leakage: intermediate function calls and tool parameters are excluded.
    """
    completed_turns: list[CompletedTurn] = []
    current_user_query: str | None = None
    current_user_evt: Any = None
    current_asst_events: list[Any] = []

    def _finalize_turn() -> None:
        """Finalize the current dialogue turn and append it to completed turns.

        Extracts assistant response text or research summary, associates it with
        the pending user query, and resets turn accumulation buffers.
        """
        nonlocal current_user_query, current_user_evt, current_asst_events
        if not current_user_query or current_user_evt is None:
            current_user_query = None
            current_user_evt = None
            current_asst_events = []
            return

        user_inv_id = getattr(current_user_evt, "invocation_id", None)
        if not user_inv_id:
            for a_evt in current_asst_events:
                if getattr(a_evt, "invocation_id", None):
                    user_inv_id = getattr(a_evt, "invocation_id", None)
                    break

        asst_response = ""

        # 1. Check for matching research pipeline turn in RESEARCH_TURN_HISTORY
        matched_entry: dict[str, Any] | None = None
        if research_history:
            if user_inv_id:
                for entry in research_history:
                    if isinstance(entry, dict) and entry.get("invocation_id") == user_inv_id:
                        matched_entry = entry
                        break
            if not matched_entry:
                for entry in research_history:
                    if isinstance(entry, dict) and entry.get("user_query") == current_user_query:
                        matched_entry = entry
                        break

        if matched_entry:
            conclude_out = matched_entry.get("conclude_output")
            summary = ""
            if isinstance(conclude_out, dict):
                summary = str(
                    conclude_out.get("summary") or conclude_out.get("notes") or ""
                ).strip()
                if not summary and "selected_candidates" in conclude_out:
                    cands = conclude_out.get("selected_candidates") or []
                    cand_texts = [
                        f"{c.get('title', '')}: {c.get('tldr', '')}" if c.get("tldr") else c.get("title", "")
                        for c in cands
                        if isinstance(c, dict) and c.get("title")
                    ]
                    summary = "; ".join(cand_texts[:3])
            elif isinstance(conclude_out, str):
                summary = conclude_out.strip()

            if summary:
                words = summary.split()
                if len(words) > 150:
                    summary = " ".join(words[:150]) + "..."
                asst_response = summary

        # 2. Conversational turn fallback (untruncated coordinator response, thoughts excluded)
        if not asst_response and current_asst_events:
            texts: list[str] = []
            for evt in current_asst_events:
                txt, _, _, _ = extract_event_parts(evt)
                if txt.strip():
                    texts.append(txt.strip())
            asst_response = "\n\n".join(texts).strip()

        if current_user_query and asst_response:
            completed_turns.append(
                CompletedTurn(
                    user_query=current_user_query,
                    assistant_response=asst_response,
                    invocation_id=user_inv_id,
                )
            )

        current_user_query = None
        current_user_evt = None
        current_asst_events = []

    for evt in events:
        author = getattr(evt, "author", "assistant")
        txt, _, _, _ = extract_event_parts(evt)
        is_user = (author == "user") and bool(txt.strip())

        if is_user:
            _finalize_turn()
            current_user_query = txt.strip()
            current_user_evt = evt
            current_asst_events = []
        else:
            if current_user_evt is not None:
                current_asst_events.append(evt)

    _finalize_turn()
    return completed_turns


def get_completed_turns(
    session: Any,
    since_last_compaction: bool = False,
) -> list[CompletedTurn]:
    """Extract all completed turns from session events, respecting rewinds and isolation scopes."""
    raw_events = (
        getattr(session, "events", [])
        if hasattr(session, "events")
        else (session if isinstance(session, list) else [])
    )
    live_events = _safe_apply_rewinds(raw_events)

    last_compaction_end_ts: float | None = None
    if since_last_compaction:
        for e in reversed(live_events):
            actions = getattr(e, "actions", None)
            comp = getattr(actions, "compaction", None) if actions else None
            if comp is not None:
                end_ts = getattr(comp, "end_timestamp", None)
                if end_ts is not None:
                    try:
                        last_compaction_end_ts = float(end_ts)
                        break
                    except (ValueError, TypeError):
                        pass

    unscoped_events = [
        e
        for e in live_events
        if getattr(e, "isolation_scope", None) is None
        and not _is_compaction_event(e)
    ]

    if last_compaction_end_ts is not None:
        unscoped_events = [
            e
            for e in unscoped_events
            if getattr(e, "timestamp", 0.0) > last_compaction_end_ts
        ]

    state_dict = (
        getattr(session, "state", {})
        if hasattr(session, "state")
        else (session.get("state", {}) if isinstance(session, dict) else {})
    )
    research_history = (
        state_dict.get(RESEARCH_TURN_HISTORY) if isinstance(state_dict, dict) else None
    )

    return _extract_completed_turns_from_events(unscoped_events, research_history)


def count_completed_turns_since_last_compaction(session: Any) -> int:
    """Count completed dialogue turns since the latest compaction event (or start of session)."""
    return len(get_completed_turns(session, since_last_compaction=True))


def extract_decision_state(
    session: Any,
    k: int = 2,
    since_last_compaction: bool = False,
) -> dict[str, str]:
    """Extract clean serial Markdown dialogue state partitioned into earlier_discussion and latest_turn.

    - Turns 1 .. N-1 are mapped to `earlier_discussion`.
    - Turn N is mapped to `latest_turn`.
    - Returns {"earlier_discussion": earlier_discussion, "latest_turn": latest_turn}.
    """
    completed_turns = get_completed_turns(session, since_last_compaction=since_last_compaction)
    if len(completed_turns) < 2:
        if len(completed_turns) == 1:
            return {
                "earlier_discussion": "",
                "latest_turn": _format_turn(
                    completed_turns[0].user_query,
                    completed_turns[0].assistant_response,
                ),
            }
        return {
            "earlier_discussion": "",
            "latest_turn": "",
        }

    earlier_discussion = "\n\n".join(
        _format_turn(t.user_query, t.assistant_response)
        for t in completed_turns[:-1]
    )
    latest_turn = _format_turn(
        completed_turns[-1].user_query,
        completed_turns[-1].assistant_response,
    )
    return {
        "earlier_discussion": earlier_discussion,
        "latest_turn": latest_turn,
    }


def evaluate_adaptive_compaction_rubric(
    state: dict[str, str],
    cfg: dict[str, Any],
    api_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> float | None:
    """Evaluate whether earlier_discussion warrants compaction via the decision model rubric probe.

    Queries OpenRouter Decisions API or local Laya with a 10.0s timeout and a 250ms retry on HTTP 429.
    Wrapped in OpenTelemetry span `compaction_decision_probe` (kind=EVALUATOR). Fails open to None on any error.
    """
    model = str(
        cfg.get("adaptive_compaction_decision_model")
        or DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL
    )
    threshold = float(
        cfg.get("adaptive_compaction_threshold", DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD)
    )
    timeout = float(cfg.get("adaptive_compaction_timeout_sec", DEFAULT_TIMEOUT_SEC))

    earlier_disc = state.get("earlier_discussion", "")
    latest = state.get("latest_turn", "")
    payload = {
        "model": model,
        "state": {
            "earlier_discussion": earlier_disc,
            "latest_turn": latest,
        },
        "questions": {
            "compaction_warranted": {
                "type": "noul",
                "instructions": DECISION_PROMPT_INSTRUCTIONS,
            }
        },
    }

    if is_local_model(model):
        provider = "local_laya"
        target_url = cfg.get(
            "citation_verification_local_url", DEFAULT_LOCAL_URL
        )
        parsed = urllib.parse.urlparse(target_url)
        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        if not probe_local_laya_health(health_url):
            logger.warning(
                "Local Laya health probe failed at %s. Failing open.", health_url
            )
            return None
        headers = {"Content-Type": "application/json"}
    else:
        provider = "openrouter"
        target_url = cfg.get("openrouter_decisions_url", OPENROUTER_DECISIONS_URL)
        resolved_key = api_key or get_secure_api_key(
            "openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")
        )
        if not resolved_key:
            logger.warning(
                "OpenRouter API key not configured for adaptive compaction probe. Failing open."
            )
            return None
        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
            "X-OpenRouter-Title": "Zocrates Adaptive Compaction",
        }

    with _decision_span("compaction_decision_probe") as span:
        _safe_set(span, "openinference.span.kind", "EVALUATOR")
        _safe_set(span, "input.mime_type", "application/json")
        _safe_set(span, "input.value", json.dumps(payload, ensure_ascii=False))
        _safe_set(span, "compaction.decision.model", model)
        _safe_set(span, "compaction.decision.provider", provider)
        _safe_set(span, "compaction.decision.threshold", threshold)
        if metadata:
            _safe_set(span, "metadata", json.dumps(metadata, ensure_ascii=False))

        try:
            t0 = time.perf_counter()
            resp = requests.post(
                target_url, headers=headers, json=payload, timeout=timeout
            )
            if resp.status_code == 429:
                logger.warning(
                    "Adaptive compaction probe received HTTP 429 rate limit. Retrying after 250ms..."
                )
                time.sleep(DEFAULT_RETRY_BACKOFF_SEC)
                t0 = time.perf_counter()
                resp = requests.post(
                    target_url, headers=headers, json=payload, timeout=timeout
                )

            latency_ms = (time.perf_counter() - t0) * 1000.0
            _safe_set(span, "compaction.decision.latency_ms", round(latency_ms, 2))

            if resp.status_code != 200:
                logger.warning(
                    "Adaptive compaction probe HTTP %d from %s: %s. Failing open.",
                    resp.status_code,
                    target_url,
                    resp.text,
                )
                _safe_set(span, "compaction.decision.status", f"http_{resp.status_code}")
                _safe_set(span, "compaction.decision.verdict", "CONTINUE")
                return None

            res_data = resp.json()
            answers = res_data.get("answers", {})
            compaction_ans = answers.get("compaction_warranted", {})
            noul_val = compaction_ans.get("noul")
            if noul_val is None:
                logger.warning(
                    "Malformed response from decision model (missing answers.compaction_warranted.noul): %s. Failing open.",
                    res_data,
                )
                _safe_set(span, "compaction.decision.status", "malformed_response")
                _safe_set(span, "compaction.decision.verdict", "CONTINUE")
                return None

            prob = float(noul_val)
            if not math.isfinite(prob):
                logger.warning("Non-finite probability returned: %s. Failing open.", prob)
                _safe_set(span, "compaction.decision.status", "non_finite_probability")
                _safe_set(span, "compaction.decision.verdict", "CONTINUE")
                return None

            prob = max(0.0, min(1.0, prob))
            verdict = "COMPRESS" if prob >= threshold else "CONTINUE"

            _safe_set(span, "compaction.decision.probability", prob)
            _safe_set(span, "compaction.decision.verdict", verdict)
            _safe_set(span, "compaction.decision.status", "success")
            _safe_set(span, "output.mime_type", "application/json")
            _safe_set(span, "output.value", json.dumps(answers, ensure_ascii=False))

            if _StatusCode is not None and span is not None:
                span.set_status(_StatusCode.OK)

            return prob

        except requests.Timeout:
            logger.warning(
                "Adaptive compaction decision probe timed out (%.1fs). Failing open.",
                timeout,
            )
            _safe_set(span, "compaction.decision.status", "timeout")
            _safe_set(span, "compaction.decision.verdict", "CONTINUE")
            if _StatusCode is not None and span is not None:
                span.set_status(_StatusCode.ERROR, "timeout")
            return None
        except Exception as exc:
            logger.warning(
                "Adaptive compaction decision probe error: %s. Failing open.", exc
            )
            _safe_set(span, "compaction.decision.status", "error")
            _safe_set(span, "compaction.decision.verdict", "CONTINUE")
            if _StatusCode is not None and span is not None:
                span.set_status(_StatusCode.ERROR, str(exc))
            return None


def should_trigger_adaptive_compaction(
    session: Any,
    cfg: dict[str, Any],
    api_key: str | None = None,
) -> tuple[bool, str]:
    """Evaluate whether adaptive compaction should trigger post-turn for the given session.

    Enforces:
    1. Global enable toggle (`enable_adaptive_compaction`).
    2. Minimum completed turns invariant: `effective_min_turns = max(configured_min_turns, k + 1)`.
    3. Coordinator prompt token envelope (token floor and ceiling):
       - If tokens >= token_ceiling (default 100k): forces COMPRESS immediately without probing.
       - If tokens < token_floor (default 30k): skips compaction without probing.
    4. Fast decision model rubric probe on state (Returns COMPRESS if p >= threshold, else CONTINUE).
    Fails open to CONTINUE on any probe error or timeout.
    """
    if not cfg.get("enable_adaptive_compaction", DEFAULT_ENABLE_ADAPTIVE_COMPACTION):
        return False, "adaptive_compaction_disabled"

    k = max(
        0,
        int(
            cfg.get(
                "adaptive_compaction_retention_turns",
                DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
            )
        ),
    )
    raw_min_turns = int(
        cfg.get(
            "adaptive_compaction_min_turns", DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS
        )
    )
    effective_min_turns = max(raw_min_turns, k + 1)
    if raw_min_turns <= k:
        logger.warning(
            "adaptive_compaction_min_turns (%d) must be strictly greater than "
            "retention_turns (%d). Disambiguating by clamping effective_min_turns to %d.",
            raw_min_turns,
            k,
            effective_min_turns,
        )

    completed_turns = count_completed_turns_since_last_compaction(session)
    if completed_turns < effective_min_turns:
        return False, f"turn_count_below_min ({completed_turns} < {effective_min_turns})"

    raw_events = (
        getattr(session, "events", [])
        if hasattr(session, "events")
        else (session if isinstance(session, list) else [])
    )
    live_events = _safe_apply_rewinds(raw_events)
    tokens = get_coordinator_prompt_tokens(live_events)
    token_ceiling = int(
        cfg.get(
            "adaptive_compaction_token_ceiling",
            DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING,
        )
    )
    token_floor = int(
        cfg.get(
            "adaptive_compaction_token_floor",
            DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR,
        )
    )

    if tokens is not None and tokens >= token_ceiling:
        return True, f"token_ceiling_exceeded ({tokens} >= {token_ceiling})"

    if tokens is not None and tokens < token_floor:
        return False, f"tokens_below_floor ({tokens} < {token_floor})"

    state = extract_decision_state(session, k=k, since_last_compaction=True)
    if not state.get("earlier_discussion") or not state.get("latest_turn"):
        return False, "insufficient_dialogue_state"

    threshold = float(
        cfg.get("adaptive_compaction_threshold", DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD)
    )
    metadata = {
        "completed_turns": completed_turns,
        "coordinator_prompt_tokens": tokens,
        "threshold": threshold,
    }
    session_id = getattr(session, "id", None) or getattr(session, "session_id", None)
    if session_id:
        metadata["session_id"] = str(session_id)

    prob = evaluate_adaptive_compaction_rubric(
        state, cfg, api_key=api_key, metadata=metadata
    )
    if prob is None:
        return False, "probe_failed_or_timed_out"

    if prob >= threshold:
        return True, f"decision_probe_triggered (p={prob:.3f} >= {threshold:.2f})"
    else:
        return False, f"decision_probe_declined (p={prob:.3f} < {threshold:.2f})"
