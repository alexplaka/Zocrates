"""Dynamic online literature discovery pipeline: search loop and synthesis workflow node."""

import logging
import re
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.tools import ToolContext
from google.adk.workflow import node
from google.adk.workflow._errors import NodeInterruptedError

from zocrates.agents.common.state_keys import (
    CANDIDATE_PAPERS,
    HITL_SNAPSHOT,
    RESEARCH_TURN_HISTORY,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_EXTENSION_REQUEST_PASS,
    TEMP_ONLINE_CANDIDATES,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_SEARCH_QUERY,
    TEMP_SEARCH_REPORT,
    USER_PROMPT,
    USER_RESEARCH_PROFILE_STR,
)
from zocrates.agents.online_search.agent import (
    create_online_search_agent,
    create_online_synthesizer_agent,
)
from zocrates.agents.online_search.tools import (
    INITIAL_ONLINE_TOOL_BUDGET,
    MAX_ONLINE_EXTENSIONS,
    ONLINE_BUDGET_EXTENSION_INCREMENT,
)
from zocrates.agents.common.pipeline_base import (
    coerce_to_text,
    enqueue_isolation_scope_closing_event,
    ensure_isolation_scope,
    get_pipeline_interrupt_ids,
    recover_isolation_scope,
)
from zocrates.discovery.models import CandidatePaper
from zocrates.config import load_zocrates_config

logger = logging.getLogger(__name__)


def _recover_isolation_scope(ctx: Context) -> str | None:
    """Recover online search pipeline's isolation scope from snapshot or session events if unset."""
    return recover_isolation_scope(
        ctx,
        prefixes=("search_online_pipeline:",),
    )


def _ensure_isolation_scope(ctx: Context) -> str | None:
    """Recover and assign isolation scope to ctx and its invocation context if unset."""
    return ensure_isolation_scope(
        ctx,
        prefixes=("search_online_pipeline:",),
    )


def _get_pipeline_interrupt_ids(ctx: Context) -> set[str]:
    """Scan session events and ctx.resume_inputs to return set of all pipeline interrupt IDs."""
    return get_pipeline_interrupt_ids(ctx)


async def _enqueue_isolation_scope_closing_event(
    ctx: Context,
    *,
    result: str,
) -> None:
    """Emit a terminal finish_task event that closes ctx's isolation scope."""
    await enqueue_isolation_scope_closing_event(
        ctx,
        result=result,
        default_author="search_online_pipeline_node",
    )


@node(name="online_search_budget_checkpoint", rerun_on_resume=False)
async def online_search_budget_checkpoint(
    ctx: Context,
    node_input: dict[str, Any] | None = None,
) -> Any:
    """Dynamic workflow checkpoint node yielding RequestInput to pause for human extension."""
    payload = node_input or {}
    ext_num = payload.get("extension_number", 1)
    int_id = f"interrupt_ext_{ext_num}"
    yield RequestInput(
        interrupt_id=int_id,
        message="Online literature discovery limit reached. Extend search or synthesize current findings?",
        payload=payload,
    )


def format_candidates_for_synthesizer(candidates: list[CandidatePaper]) -> str:
    """Format candidate papers into an ordered list with sequential 1-based indices."""
    if not candidates:
        return "No candidate papers found."

    blocks = []
    for i, c in enumerate(candidates, 1):
        authors_str = ", ".join(c.authors) if c.authors else "Unknown"
        venue_str = c.venue or "Unknown Venue"
        oa_str = "Yes" if c.is_oa else "No"
        abstract_str = c.abstract or c.tldr or "No abstract or summary available."
        keywords_str = ", ".join(c.keywords) if c.keywords else "None"
        topic_str = c.primary_topic or "N/A"

        block = (
            f"Candidate [{i}]\n"
            f"- Candidate ID: {c.candidate_id}\n"
            f"- Title: {c.title}\n"
            f"- Authors: {authors_str}\n"
            f"- Year: {c.year or 'Unknown'}\n"
            f"- Venue: {venue_str} | Citations: {c.citation_count} | Open Access: {oa_str}\n"
            f"- DOI: {c.doi or 'None'} | arXiv ID: {c.arxiv_id or 'None'}\n"
            f"- Primary Topic: {topic_str}\n"
            f"- Keywords: {keywords_str}\n"
            f"- Abstract: {abstract_str}"
        )
        blocks.append(block)

    return "\n\n".join(blocks)


def _format_candidate_authors(raw_authors: Any) -> str:
    """Format a candidate paper's authors into a clean academic citation string."""
    if isinstance(raw_authors, list):
        cleaned = [str(a).strip() for a in raw_authors if a and str(a).strip()]
    elif isinstance(raw_authors, str) and raw_authors.strip():
        cleaned = [raw_authors.strip()]
    else:
        cleaned = []

    if not cleaned:
        return "Unknown Authors"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} & {cleaned[1]}"
    if len(cleaned) == 3:
        return f"{cleaned[0]}, {cleaned[1]}, & {cleaned[2]}"
    return f"{cleaned[0]} et al."


def build_online_coordinator_view(
    text: str,
    candidates: list[CandidatePaper] | None,
) -> str:
    """Build a clean Markdown coordinator view with a trailing candidate papers mapping.

    Appends a `# Discovered Candidate Papers` bibliography mapping 1-based candidate
    indices (`[1]`, `[2]`, ...) to bibliographic details and import rationales while
    omitting full abstracts to bound coordinator context token usage.
    """
    # Strip trailing candidate paper / reference headings for idempotency
    clean_text = re.sub(
        r"(?:\n{1,3}|^\s*)#{1,3}\s*(?:References|Cited Sources|Sources Consulted|Recommended Papers for Import|Discovered Candidate Papers)[\s\S]*$",
        "",
        text or "",
        flags=re.IGNORECASE,
    ).strip()
    if not candidates:
        return clean_text

    lines = ["# Discovered Candidate Papers"]
    for i, raw_c in enumerate(candidates, 1):
        extra_pdf_url = raw_c.get("pdf_url") if isinstance(raw_c, dict) else None
        c = CandidatePaper.from_dict(raw_c) if isinstance(raw_c, dict) else raw_c

        authors_str = _format_candidate_authors(getattr(c, "authors", None))
        year_str = str(c.year).strip() if getattr(c, "year", None) else "n.d."
        raw_title = str(getattr(c, "title", None) or "").strip()
        title_str = raw_title.rstrip(".") if raw_title else "Untitled"

        raw_venue = str(getattr(c, "venue", None) or "").strip().rstrip(".")
        venue_str = f" In {raw_venue}." if raw_venue else ""

        raw_doi = str(getattr(c, "doi", None) or "").strip()
        raw_url = str(
            getattr(c, "oa_pdf_url", None)
            or getattr(c, "pdf_url", None)
            or extra_pdf_url
            or ""
        ).strip()
        if raw_doi:
            doi_str = f" DOI: {raw_doi}"
        elif raw_url:
            doi_str = f" URL: {raw_url}"
        else:
            doi_str = ""

        entry = f"- [{i}] {authors_str} ({year_str}). {title_str}.{venue_str}{doi_str}"
        raw_tldr = str(getattr(c, "tldr", None) or "").strip()
        if raw_tldr:
            entry += f"\n  - Rationale: {raw_tldr}"

        lines.append(entry)

    candidate_section = "\n".join(lines)
    if clean_text:
        return f"{clean_text}\n\n{candidate_section}"
    return candidate_section


def _strip_thinking_tags(text: str) -> str:
    """Safely strip reasoning/thinking scratchpad tags without mangling code blocks or inline references."""
    if not text or ("<think>" not in text and "</think>" not in text):
        return text

    code_blocks: list[str] = []

    def _save_code(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE_{len(code_blocks)-1}\x00"

    # Protect code blocks and inline backticks
    protected = re.sub(r"```[\s\S]*?```|`[^`\n]+`", _save_code, text)

    # 1. Strip closed thinking blocks outside code
    protected = re.sub(r"<think>[\s\S]*?</think>", "", protected)

    # 2. Strip orphaned closing tag at start of output (pre-injected prompt template remnants)
    protected = re.sub(r"^\s*[\s\S]*?</think>\s*", "", protected)

    # 3. Only strip unclosed <think> if it starts at the beginning of the text (token truncation of scratchpad)
    protected = re.sub(r"^\s*<think>[\s\S]*$", "", protected)

    # Restore code blocks
    def _restore_code(m: re.Match) -> str:
        idx = int(m.group(1))
        return code_blocks[idx]

    return re.sub(r"\x00CODE_(\d+)\x00", _restore_code, protected)


def _clean_user_facing_synthesis(text: str) -> str:
    """Post-process online synthesizer output to deterministically strip thinking tags and bibliography headings."""
    if not text:
        return text

    # Strip thinking traces safely without mangling code blocks or legitimate inline mentions
    text = _strip_thinking_tags(text)

    # Strip hallucinated trailing references / candidate paper headings
    cleaned = re.sub(
        r"(?:\n{1,3}|^\s*)#{1,3}\s*(?:References|Cited Sources|Sources Consulted|Recommended Papers for Import|Discovered Candidate Papers)[\s\S]*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).rstrip()

    return cleaned.strip()


@node(name="search_online_pipeline_node", rerun_on_resume=True)
async def search_online_pipeline_node(
    ctx: Context,
    node_input: str = "",
) -> str:
    """Dynamic workflow function orchestrating online literature search and synthesis.

    Wraps ``_run_online_search_pipeline`` in a try/finally so the node always closes
    its isolation scope (``search_online_pipeline:{fc_id}``) before returning, whether
    it succeeds ("Task completed.") or raises ("Task failed.").
    """
    _ensure_isolation_scope(ctx)

    completed = False
    interrupted = False
    try:
        response_text = await _run_online_search_pipeline(ctx, node_input)
        completed = True
        return response_text
    except NodeInterruptedError:
        interrupted = True
        raise
    finally:
        if not interrupted:
            await _enqueue_isolation_scope_closing_event(
                ctx,
                result="Task completed." if completed else "Task failed.",
            )


def _reconcile_state_after_subagent(state: Any) -> None:
    """Reconcile uncommitted parent State._delta with child subagent updates in State._value."""
    delta = getattr(state, "_delta", None)
    value = getattr(state, "_value", None)
    if not isinstance(delta, dict) or not isinstance(value, dict):
        return
    for key in list(delta.keys()):
        if key in value and delta[key] is not value[key] and delta[key] != value[key]:
            delta.pop(key, None)


async def _run_online_search_pipeline(ctx: Context, node_input: str) -> str:
    """Core online search pipeline: subagent execution, HITL extensions, and synthesis."""
    state = ctx.state
    snapshot = state.get(HITL_SNAPSHOT)
    if snapshot and snapshot.get("search_query"):
        search_query = snapshot["search_query"]
    else:
        raw_query = node_input or state.get(TEMP_SEARCH_QUERY, "")
        search_query = re.sub(
            r"\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】",
            "",
            raw_query,
        ).strip()
    state[TEMP_SEARCH_QUERY] = search_query

    cfg = load_zocrates_config()
    h_cfg = cfg.get("search_harness") or {}
    hitl_enabled = bool(h_cfg.get("hitl_enabled", True))
    initial_online_budget = int(
        h_cfg.get("online_initial_retrieval_tools", INITIAL_ONLINE_TOOL_BUDGET)
    )
    max_extensions = int(h_cfg.get("max_online_extensions", MAX_ONLINE_EXTENSIONS))
    budget_increment = int(h_cfg.get("online_budget_increment", ONLINE_BUDGET_EXTENSION_INCREMENT))

    completed_extensions: list[int] = (
        list(snapshot.get("completed_extensions", []))
        if (snapshot and isinstance(snapshot, dict))
        else []
    )
    existing_interrupt_ids = _get_pipeline_interrupt_ids(ctx)

    if not snapshot:
        state[TEMP_RETRIEVAL_TOOL_BUDGET] = initial_online_budget
        state["temp_retrieval_tool_budget"] = initial_online_budget
        state[TEMP_RETRIEVAL_TOOL_COUNT] = 0
        state["temp_retrieval_tool_count"] = 0
        state[TEMP_EXTENSION_COUNT] = 0
        state["temp_extension_count"] = 0
        state[TEMP_ONLINE_CANDIDATES] = {}
        state["temp_online_candidates"] = {}
        state["selected_candidates"] = []
        state["candidates"] = []
        state["candidates_invocation_id"] = None
        state[CANDIDATE_PAPERS] = []
        state[TEMP_SEARCH_REPORT] = None
        state["temp_online_search_report"] = None
        state[TEMP_EXTENSION_REQUESTED] = False
        state["temp_extension_requested"] = False
        state[TEMP_EXTENSION_REQUEST_PASS] = None
        state["extension_rationale"] = None
        raw_engine = state.get("discovery_engine")
        if not (hasattr(raw_engine, "search") and callable(getattr(raw_engine, "search"))):
            if hasattr(state, "pop"):
                state.pop("discovery_engine", None)
            elif raw_engine is not None:
                state["discovery_engine"] = None
    else:
        ceilings = snapshot.get("ceilings", {})
        if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is None:
            state[TEMP_RETRIEVAL_TOOL_BUDGET] = ceilings.get("budget", INITIAL_ONLINE_TOOL_BUDGET)
        if not state.get(TEMP_ONLINE_CANDIDATES) and snapshot.get("candidates_pool"):
            state[TEMP_ONLINE_CANDIDATES] = snapshot["candidates_pool"]
            state["temp_online_candidates"] = snapshot["candidates_pool"]
        if not state.get("selected_candidates") and snapshot.get("selected_candidates"):
            restored = [
                CandidatePaper.from_dict(c) if isinstance(c, dict) else c
                for c in snapshot["selected_candidates"]
            ]
            state["selected_candidates"] = restored
            state[CANDIDATE_PAPERS] = [
                c.to_dict() if hasattr(c, "to_dict") else c for c in restored
            ]

    if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is None:
        state[TEMP_RETRIEVAL_TOOL_BUDGET] = initial_online_budget
    if state.get(TEMP_EXTENSION_COUNT) is None:
        state[TEMP_EXTENSION_COUNT] = 0

    # Clear invocation user_content so subagents receive only dedicated input
    if hasattr(ctx, "_invocation_context") and ctx._invocation_context is not None:
        ctx._invocation_context.user_content = None

    # Subagent resolution
    parent_agent = (
        getattr(ctx._invocation_context, "agent", None)
        if hasattr(ctx, "_invocation_context")
        else None
    )
    subagents = getattr(parent_agent, "_subagents", {}) if parent_agent else {}
    search_agent = subagents.get("online_search")
    synth_agent = subagents.get("online_synthesizer")

    if (search_agent is None or synth_agent is None) and getattr(parent_agent, "name", None) in (
        "search_online_pipeline_node",
    ):
        try:
            from zocrates.agents.app import coordinator_agent

            if coordinator_agent and hasattr(coordinator_agent, "_subagents"):
                coord_subagents = getattr(coordinator_agent, "_subagents", {})
                if search_agent is None:
                    search_agent = coord_subagents.get("online_search")
                if synth_agent is None:
                    synth_agent = coord_subagents.get("online_synthesizer")
        except Exception:
            pass

    if search_agent is None or synth_agent is None:
        from zocrates.llm import get_llm_model

        default_model = get_llm_model()
        if search_agent is None:
            search_agent = create_online_search_agent(default_model)
        if synth_agent is None:
            synth_agent = create_online_synthesizer_agent(default_model)

    search_node_input = f"# Target Literature Discovery Goal\n{search_query}"
    active_isolation_scope = _ensure_isolation_scope(ctx)

    curr_inv_id = getattr(ctx, "invocation_id", None) or (
        getattr(ctx._invocation_context, "invocation_id", None)
        if hasattr(ctx, "_invocation_context")
        else None
    )
    session = getattr(ctx, "session", None)
    if not session and hasattr(ctx, "_invocation_context"):
        session = getattr(ctx._invocation_context, "session", None)
    if not curr_inv_id and session and getattr(session, "events", None):
        for ev in reversed(session.events):
            if getattr(ev, "invocation_id", None):
                curr_inv_id = ev.invocation_id
                break

    has_initial_run = bool(snapshot)
    if not has_initial_run and curr_inv_id:
        if session and getattr(session, "events", None):
            for ev in session.events:
                if getattr(ev, "invocation_id", None) == curr_inv_id:
                    node_path = getattr(getattr(ev, "node_info", None), "path", "") or ""
                    if "search_initial" in node_path:
                        has_initial_run = True
                        break

    if not has_initial_run:
        await ctx.run_node(
            search_agent,
            node_input=search_node_input,
            run_id="search_initial",
            override_isolation_scope=active_isolation_scope,
        )
        _reconcile_state_after_subagent(state)

    # Extension loop
    extension_count = 0
    if snapshot and snapshot.get("extension_count") is not None:
        extension_count = int(snapshot["extension_count"])

    while True:
        pool = (
            state.get(TEMP_ONLINE_CANDIDATES)
            if state.get(TEMP_ONLINE_CANDIDATES) is not None
            else state.get("temp_online_candidates")
        ) or (snapshot.get("candidates_pool") if snapshot else {}) or {}

        search_report = state.get(TEMP_SEARCH_REPORT) or (snapshot.get("search_report") if snapshot else None)
        if search_report is None:
            search_report = {
                "summary": f"Discovered {len(pool)} candidate papers." if pool else "No candidate papers discovered.",
                "gaps": [] if pool else ["Need to formulate alternative queries to discover relevant papers."],
                "notes": "",
                "source": "auto_generated",
            }
            state[TEMP_SEARCH_REPORT] = search_report

        current_calls = int(
            state.get(TEMP_RETRIEVAL_TOOL_COUNT)
            if state.get(TEMP_RETRIEVAL_TOOL_COUNT) is not None
            else state.get("temp_retrieval_tool_count") or (snapshot.get("tool_count") if snapshot else 0) or 0
        )
        current_budget = int(
            state.get(TEMP_RETRIEVAL_TOOL_BUDGET)
            if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is not None
            else state.get("temp_retrieval_tool_budget") or INITIAL_ONLINE_TOOL_BUDGET
        )

        target_pass = extension_count + 1
        req_pass = state.get(TEMP_EXTENSION_REQUEST_PASS)
        if req_pass is not None:
            extension_requested = (req_pass == target_pass)
        else:
            extension_requested = (
                bool(state.get(TEMP_EXTENSION_REQUESTED, False))
                or bool(state.get("temp_extension_requested", False))
                or bool(snapshot.get("extension_requested", False) if snapshot else False)
            ) if extension_count == 0 else False

        gaps = search_report.get("gaps", [])

        target_int_id = f"interrupt_ext_{extension_count + 1}"
        is_known_checkpoint = target_int_id in existing_interrupt_ids
        is_historical_pass = bool(
            snapshot and extension_count < int(snapshot.get("extension_count", 0))
        ) or is_known_checkpoint

        should_trigger = (
            hitl_enabled
            and (extension_count < max_extensions)
            and (
                is_historical_pass
                or extension_requested
                or (bool(gaps) and (current_calls >= current_budget))
            )
        )

        if not should_trigger:
            break

        if is_historical_pass and snapshot and snapshot.get("trigger_reason"):
            trigger_reason = snapshot.get("trigger_reason")
        elif extension_requested or (is_historical_pass and snapshot and snapshot.get("extension_requested")):
            trigger_reason = "agent_request"
        elif current_calls >= current_budget:
            trigger_reason = "tool_budget_exhausted"
        else:
            trigger_reason = "unknown"

        agent_justification = (
            state.get("extension_rationale")
            or (
                search_report.get("notes")
                if search_report.get("source") == "subagent_request"
                else None
            )
        ) if trigger_reason == "agent_request" else None

        # Save snapshot
        serialized_pool = {
            k: (v.to_dict() if hasattr(v, "to_dict") else v)
            for k, v in pool.items()
        }
        state[HITL_SNAPSHOT] = {
            "search_query": search_query,
            "candidates_pool": serialized_pool,
            "selected_candidates": [
                c.to_dict() if hasattr(c, "to_dict") else c
                for c in state.get("selected_candidates") or []
            ],
            "search_report": search_report,
            "extension_count": extension_count,
            "tool_count": current_calls,
            "extension_requested": extension_requested,
            "isolation_scope": active_isolation_scope,
            "completed_extensions": list(completed_extensions),
            "ceilings": {
                "budget": current_budget,
            },
            "pipeline_type": "online_search",
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
            },
            "agent_justification": agent_justification,
        }

        checkpoint_payload = {
            "type": "deep_research_extension",
            "extension_number": extension_count + 1,
            "max_extensions": max_extensions,
            "current_calls": current_calls,
            "covered_summary": search_report.get("summary", ""),
            "missing_aspects": gaps,
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
            },
            "agent_justification": agent_justification,
        }

        decision = await ctx.run_node(
            online_search_budget_checkpoint,
            node_input=checkpoint_payload,
            run_id=f"checkpoint_{extension_count}",
            override_isolation_scope=active_isolation_scope,
        )
        action = decision.get("action") if isinstance(decision, dict) else str(decision or "")

        if action != "extend_budget":
            break

        new_budget = current_budget + budget_increment
        new_extension_count = extension_count + 1

        state[TEMP_RETRIEVAL_TOOL_BUDGET] = new_budget
        state["temp_retrieval_tool_budget"] = new_budget
        state[TEMP_EXTENSION_COUNT] = new_extension_count
        state["temp_extension_count"] = new_extension_count

        guidance = (decision.get("guidance") or "").strip() if isinstance(decision, dict) else ""
        gaps_list = "\n".join(f"- {g}" for g in gaps) if gaps else "- Continue targeted literature discovery"
        continuation_prompt = (
            f"# Deep Research Extension Granted (Extension {new_extension_count} of {max_extensions})\n"
            "The user granted additional retrieval tool calls for online discovery.\n\n"
            f"## Remaining Gaps to Retrieve:\n{gaps_list}\n"
        )
        if guidance:
            continuation_prompt += f"\n## User Steering Guidance:\n{guidance}\n"

        if new_extension_count not in completed_extensions:
            await ctx.run_node(
                search_agent,
                node_input=continuation_prompt,
                run_id=f"online_search_extension_{new_extension_count}",
                override_isolation_scope=active_isolation_scope,
            )
            _reconcile_state_after_subagent(state)
            completed_extensions.append(new_extension_count)

        latest_pool = (
            state.get(TEMP_ONLINE_CANDIDATES)
            or state.get("temp_online_candidates")
            or pool
            or {}
        )
        state[HITL_SNAPSHOT] = {
            "search_query": search_query,
            "candidates_pool": {
                k: (v.to_dict() if hasattr(v, "to_dict") else v)
                for k, v in latest_pool.items()
            },
            "selected_candidates": [
                c.to_dict() if hasattr(c, "to_dict") else c
                for c in state.get("selected_candidates") or []
            ],
            "search_report": state.get(TEMP_SEARCH_REPORT) or search_report,
            "extension_count": new_extension_count,
            "tool_count": int(state.get(TEMP_RETRIEVAL_TOOL_COUNT) or state.get("temp_retrieval_tool_count") or current_calls),
            "extension_requested": False,
            "isolation_scope": active_isolation_scope,
            "completed_extensions": list(completed_extensions),
            "ceilings": {
                "budget": new_budget,
            },
            "pipeline_type": "online_search",
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
            },
            "agent_justification": agent_justification,
        }
        extension_count = new_extension_count

    # Determine candidate papers to present to synthesizer
    raw_selected = (
        state.get("selected_candidates")
        or (snapshot.get("selected_candidates") if snapshot else [])
        or []
    )
    synth_candidates: list[CandidatePaper] = [
        CandidatePaper.from_dict(c) if isinstance(c, dict) else c
        for c in raw_selected
    ]

    if not synth_candidates and pool:
        for cand in pool.values():
            c = CandidatePaper.from_dict(cand) if isinstance(cand, dict) else cand
            synth_candidates.append(c)
        synth_candidates = synth_candidates[:10]

    formatted_candidates = format_candidates_for_synthesizer(synth_candidates)

    user_query = state.get(USER_PROMPT)
    if not user_query:
        if session and getattr(session, "events", None) and curr_inv_id:
            for ev in session.events:
                if (
                    getattr(ev, "invocation_id", None) == curr_inv_id
                    and getattr(ev, "author", None) == "user"
                    and getattr(ev, "content", None)
                ):
                    parts = getattr(ev.content, "parts", None) or []
                    for p in parts:
                        if getattr(p, "text", None) and not getattr(p, "thought", False):
                            user_query = p.text.strip()
                            break
                    if user_query:
                        break
    if not user_query:
        user_query = search_query

    # Record research turn entry for predictive prompt continuity
    research_entry = {
        "invocation_id": curr_inv_id,
        "pipeline": "online_search",
        "user_query": user_query,
        "search_goal": search_query,
        "conclude_output": {
            "summary": search_report.get("summary", ""),
            "gaps": search_report.get("gaps", []),
            "selected_candidates": [
                {"title": getattr(c, "title", ""), "tldr": getattr(c, "tldr", "")}
                for c in synth_candidates
                if getattr(c, "title", None)
            ],
        },
    }
    history = list(state.get(RESEARCH_TURN_HISTORY) or [])
    if history and history[-1].get("invocation_id") == curr_inv_id:
        history[-1] = research_entry
    else:
        history.append(research_entry)
    state[RESEARCH_TURN_HISTORY] = history[-5:]

    profile_str = str(state.get(USER_RESEARCH_PROFILE_STR) or "").strip()
    synth_prompt_parts = []
    if profile_str:
        synth_prompt_parts.append(profile_str)
    synth_prompt_parts.extend([
        f"# User Research Inquiry\n{user_query}",
        f"# Target Search Goal\n{search_query}",
        f"# Discovered Candidate Papers\n{formatted_candidates}",
    ])
    synth_prompt = "\n\n".join(synth_prompt_parts)

    synth_response = await ctx.run_node(
        synth_agent,
        node_input=synth_prompt,
        use_as_output=True,
        override_isolation_scope=active_isolation_scope,
    )
    raw_synth_text = coerce_to_text(synth_response)

    # Programmatic Candidate Stamping
    raw_candidates = (
        state.get("selected_candidates")
        or (snapshot.get("selected_candidates") if snapshot else [])
        or []
    )
    if not raw_candidates and pool:
        raw_candidates = list(pool.values())[:10]

    candidate_dicts = [
        c.to_dict() if hasattr(c, "to_dict") else c
        for c in raw_candidates
    ]

    state["candidates"] = candidate_dicts
    state["selected_candidates"] = candidate_dicts
    state[CANDIDATE_PAPERS] = candidate_dicts
    state["candidates_invocation_id"] = curr_inv_id
    meta = dict(state.get("metadata") or {})
    meta["candidates"] = candidate_dicts
    state["metadata"] = meta

    # Turn cleanup
    state[HITL_SNAPSHOT] = None
    state[TEMP_EXTENSION_REQUESTED] = False
    state["temp_extension_requested"] = False
    state[TEMP_EXTENSION_REQUEST_PASS] = None
    state[TEMP_EXTENSION_COUNT] = None
    state["temp_extension_count"] = None
    state[TEMP_RETRIEVAL_TOOL_BUDGET] = None
    state["temp_retrieval_tool_budget"] = None
    state["extension_rationale"] = None

    cleaned_synth_text = _clean_user_facing_synthesis(raw_synth_text)
    coordinator_view = build_online_coordinator_view(cleaned_synth_text, synth_candidates)
    return coordinator_view


async def search_online_literature(request: str, tool_context: ToolContext) -> str:
    """Autonomous discovery tool executing the online scholarly literature search pipeline.

    Use this tool when the user wants to discover new papers, preprints, external literature,
    or resolve external DOIs/arXiv links not in their library.
    """
    tool_context.actions.skip_summarization = True
    fc_id = (
        getattr(tool_context, "function_call_id", None)
        or getattr(tool_context, "invocation_id", None)
        or getattr(getattr(tool_context, "_invocation_context", None), "invocation_id", None)
        or "default"
    )
    result = await tool_context.run_node(
        search_online_pipeline_node,
        node_input=request,
        override_isolation_scope=f"search_online_pipeline:{fc_id}",
    )
    return coerce_to_text(result)


__all__ = [
    "build_online_coordinator_view",
    "format_candidates_for_synthesizer",
    "online_search_budget_checkpoint",
    "search_online_literature",
    "search_online_pipeline_node",
]
