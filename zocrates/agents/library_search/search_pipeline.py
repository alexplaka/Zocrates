"""Dynamic search pipeline: retrieval loop and synthesis workflow node for Dialogue mode."""

import logging
import re
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.tools import FunctionTool, ToolContext
from google.adk.workflow import node
from google.adk.workflow._errors import NodeInterruptedError
from google.genai import types

from zocrates.agents.common.config import (
    MAX_INJECTED_TOC_ITEMS,
    get_agent_generate_content_config,
)
from zocrates.agents.common.prompts import (
    format_active_items,
    format_retrieved_evidence_str,
    strip_snippet_keys,
)
from zocrates.agents.common.state_keys import (
    ACTIVE_CONTEXT_ITEMS,
    ACTIVE_CONTEXT_ITEMS_STR,
    CACHED_TOCS,
    HITL_SNAPSHOT,
    RESEARCH_TURN_HISTORY,
    RETRIEVED_EVIDENCE,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_EXTENSION_REQUEST_PASS,
    TEMP_MAX_RERANK_CHUNKS,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEARCH_QUERY,
    TEMP_SEARCH_REPORT,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
    TEMP_WORD_CEILING,
    USER_PROMPT,
    USER_RESEARCH_PROFILE_STR,
)
from zocrates.agents.common.pipeline_base import (
    coerce_to_text,
    enqueue_isolation_scope_closing_event,
    ensure_isolation_scope,
    get_pipeline_interrupt_ids,
    recover_isolation_scope,
)
from zocrates.agents.common.tools import (
    conclude_search,
    get_document_toc,
    get_related_items,
    read_document,
    request_search_extension,
    search_corpus,
)
from zocrates.agents.common.reranker import (
    filter_substantive_evidence,
    rerank_evidence,
)
from zocrates.agents.library_search.citation_resolver import build_coordinator_view
from zocrates.agents.library_search.prompts import (
    LIBRARY_SEARCH_AGENT_INSTRUCTION,
    LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION,
    LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION_STANDARD,
)
from zocrates.config import (
    get_citations_enabled,
    load_zocrates_config,
    resolve_zotero_data_dir,
)

logger = logging.getLogger(__name__)


MAX_AUDIT_HISTORY_TURNS: int = 5


def create_library_search_agent(
    model: Any = None,
    thinking_level: str | None = "low",
    generate_content_config: types.GenerateContentConfig | None = None,
) -> Agent:
    """Create the stateless retrieval subagent equipped with the consolidated retrieval tool suite."""
    if model is None:
        from zocrates.llm import get_llm_model

        model = get_llm_model()

    if generate_content_config is None and thinking_level is not None:
        generate_content_config = get_agent_generate_content_config(thinking_level)

    return Agent(
        name="library_search_agent",
        model=model,
        instruction=LIBRARY_SEARCH_AGENT_INSTRUCTION,
        tools=[
            FunctionTool(search_corpus),
            FunctionTool(get_document_toc),
            FunctionTool(read_document),
            FunctionTool(get_related_items),
            FunctionTool(conclude_search),
            FunctionTool(request_search_extension),
        ],
        generate_content_config=generate_content_config,
        mode="single_turn",
        include_contents="default",
    )


def get_synthesizer_instruction(citations_enabled: bool) -> str:
    """Return the synthesizer instruction tailored to citation configuration."""
    if citations_enabled:
        return LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION
    return LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION_STANDARD


def create_library_synthesizer_agent(
    model: Any = None,
    thinking_level: str | None = "high",
    generate_content_config: types.GenerateContentConfig | None = None,
    citations_enabled: bool | None = None,
) -> Agent:
    """Create the grounded literature synthesizer agent."""
    if model is None:
        from zocrates.llm import get_llm_model

        model = get_llm_model()

    if generate_content_config is None and thinking_level is not None:
        generate_content_config = get_agent_generate_content_config(thinking_level)

    if citations_enabled is None:
        try:
            citations_enabled = get_citations_enabled()
        except Exception:
            citations_enabled = True

    return Agent(
        name="library_synthesizer_agent",
        model=model,
        instruction=get_synthesizer_instruction(citations_enabled),
        generate_content_config=generate_content_config,
        mode="single_turn",
        include_contents="none",
    )


async def search_library(request: str, tool_context: ToolContext) -> str:
    """Autonomous search tool executing the local library search and synthesis pipeline.

    Use this tool when the user asks questions about papers, notes, or topics
    contained in their existing Zotero library.
    """
    tool_context.actions.skip_summarization = True
    fc_id = (
        getattr(tool_context, "function_call_id", None)
        or getattr(tool_context, "invocation_id", None)
        or getattr(getattr(tool_context, "_invocation_context", None), "invocation_id", None)
        or "default"
    )
    result = await tool_context.run_node(
        search_library_pipeline_node,
        node_input=request,
        override_isolation_scope=f"search_library_pipeline:{fc_id}",
    )
    return coerce_to_text(result)


library_search = search_library


def _auto_generate_completion_report(
    search_query: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Synthesize a completion report when `conclude_search` is not explicitly called."""
    total_words = sum(
        c.get("word_count")
        if c.get("word_count") is not None
        else len(c.get("text", "").split())
        for c in evidence
    )
    if not evidence:
        return {
            "summary": "Retrieval completed with 0 evidence chunks retrieved.",
            "notes": "No matching chunks found in active scope.",
            "gaps": [search_query],
            "source": "auto_generated",
        }

    unique_headings: list[str] = []
    seen_headings = set()
    for c in evidence:
        h = c.get("heading")
        if h and h not in seen_headings:
            seen_headings.add(h)
            unique_headings.append(h)

    chunk_types = sorted(list({c.get("chunk_type", "text") for c in evidence}))
    top_headings = unique_headings[:5]

    return {
        "summary": (
            f"Retrieval completed with {len(evidence)} evidence chunks "
            f"({', '.join(chunk_types)}) covering: {', '.join(top_headings) if top_headings else 'general content'} (~{total_words} words)."
        ),
        "notes": "Search concluded via auto-completion safeguard.",
        "gaps": [],
        "source": "auto_generated",
    }


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
    """Post-process synthesizer output to deterministically strip thinking tags, leaked internal keys, or chunk IDs."""
    if not text:
        return text

    # Strip thinking traces safely without mangling code blocks or legitimate inline mentions
    text = _strip_thinking_tags(text)

    # Strip (ITEMKEY: "Title") -> ("Title") or (ITEMKEY: Title) -> (Title)
    cleaned = re.sub(r"\([A-Z0-9]{8}:\s*([\"']?[^)]+[\"']?)\)", r"(\1)", text)

    # Strip [Key: ITEMKEY], [Attachment Key: ITEMKEY], or [Note Key: ITEMKEY]
    cleaned = re.sub(r"[\[\(](?:Attachment\s+|Note\s+)?Key:\s*[A-Za-z0-9_-]+[\]\)]\s*", "", cleaned)

    # Strip [Chunk: ITEMKEY:s1] or Chunk ITEMKEY:s1 or (Chunk: ITEMKEY:s1)
    cleaned = re.sub(r"[\[\(]?[Cc]hunk:?\s*[A-Z0-9]{8}:[a-z0-9]+[\]\)]?", "", cleaned)

    # Strip [Chunk: ITEMKEY::1] or Chunk ITEMKEY::1
    cleaned = re.sub(r"[\[\(]?[Cc]hunk:?\s*[A-Z0-9]{8}::\d+[\]\)]?", "", cleaned)

    # Clean up any resulting empty parentheses or multiple spaces
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _rewrite_relative_markdown_images(
    text: str, chunks: list[dict[str, Any]], active: Any = None
) -> str:
    """Rewrite any relative images/ Markdown image links to zocrates-img:// URIs."""
    if not text or "images/" not in text:
        return text

    file_to_key: dict[str, str] = {}
    fallback_key = None
    for c in chunks:
        k = c.get("item_key")
        if k:
            if not fallback_key:
                fallback_key = k
            img_p = c.get("image_path")
            if img_p:
                from pathlib import Path
                fname = Path(str(img_p)).name
                file_to_key[fname] = k
                file_to_key[str(img_p)] = k
    if not fallback_key and active and isinstance(active, list):
        for a in active:
            if isinstance(a, dict) and (a.get("key") or a.get("item_key")):
                fallback_key = a.get("key") or a.get("item_key")
                break

    def _repl(m: re.Match) -> str:
        """Rewrite relative Markdown image links to zocrates-img custom protocol URIs.

        Args:
            m: Regular expression match object containing image prefix and relative path.

        Returns:
            Rewritten Markdown image syntax with resolved zocrates-img URI.
        """
        prefix = m.group(1)
        rel_path = m.group(2)
        from pathlib import Path
        fname = Path(rel_path).name
        matched_key = file_to_key.get(fname) or file_to_key.get(rel_path) or fallback_key
        if matched_key:
            return f"{prefix}zocrates-img://{matched_key}/{rel_path})"
        return m.group(0)

    return re.sub(r"(!\[[^\]]*\]\()\s*(?:\./)?(images/[^)\s]+)\)", _repl, text)


def _recover_isolation_scope(ctx: Context) -> str | None:
    """Recover the search pipeline's isolation scope from snapshot or session events if unset."""
    return recover_isolation_scope(
        ctx,
        prefixes=("search_library_pipeline:", "search_pipeline:"),
    )


def _ensure_isolation_scope(ctx: Context) -> str | None:
    """Recover and assign isolation scope to ctx and its invocation context if unset."""
    return ensure_isolation_scope(
        ctx,
        prefixes=("search_library_pipeline:", "search_pipeline:"),
    )


def _get_pipeline_interrupt_ids(ctx: Context) -> set[str]:
    """Scan session events and ctx.resume_inputs to return set of all pipeline interrupt IDs."""
    return get_pipeline_interrupt_ids(ctx)


async def _enqueue_isolation_scope_closing_event(
    ctx: Context,
    *,
    result: str,
) -> None:
    """Emit a terminal ``finish_task`` event that closes ``ctx``'s isolation scope.

    ADK's ``Runner._find_active_task_scope`` treats any non-empty
    ``isolation_scope`` as an unclosed in-flight task agent awaiting user input
    unless an event carrying a ``finish_task`` FunctionResponse (result
    ``"Task completed."`` or ``"Task failed."``) is recorded for that scope.
    ``library_search`` stamps the pipeline's events with
    ``search_pipeline:{fc_id}``, so without explicit closure ADK tags
    subsequent user turns with the stale scope and the coordinator
    (``isolation_scope=None``) drops them, leaving the coordinator stuck on the
    first turn's prompt. Emitting the closing event on the invocation context
    lets ``Runner._find_active_task_scope`` recognize the scope as finished.

    Args:
        ctx: The dynamically executed search pipeline context.
        result: Terminal ``finish_task`` result ("Task completed." or "Task failed.").
    """
    await enqueue_isolation_scope_closing_event(
        ctx,
        result=result,
        default_author="search_library_pipeline_node",
    )


@node(name="search_budget_checkpoint", rerun_on_resume=False)
async def search_budget_checkpoint(
    ctx: Context,
    node_input: dict[str, Any] | None = None,
) -> Any:
    """Dynamic workflow checkpoint node yielding RequestInput to pause for human extension."""
    payload = node_input or {}
    ext_num = payload.get("extension_number", 1)
    int_id = f"interrupt_ext_{ext_num}"
    yield RequestInput(
        interrupt_id=int_id,
        message="Deep research limit reached. Extend retrieval or synthesize current findings?",
        payload=payload,
    )


def _is_item_cached(item: dict[str, Any], cached_tocs: dict[str, Any]) -> bool:
    """Check if item is cached. Primary: parent_key (input_key alias). Secondary: child attachments."""
    if not item or not isinstance(item, dict) or not cached_tocs:
        return False
    parent_key = item.get("item_key") or item.get("key")
    if parent_key and parent_key in cached_tocs:
        return True
    children = item.get("children") or []
    for child in children:
        if isinstance(child, dict):
            c_key = child.get("item_key") or child.get("key")
            if c_key and c_key in cached_tocs:
                return True
    return False


def _resolve_cached_entry(item: dict[str, Any], cached_tocs: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the cached TOC entry for an item, checking parent key then children."""
    if not item or not isinstance(item, dict) or not cached_tocs:
        return None
    parent_key = item.get("item_key") or item.get("key")
    if parent_key and parent_key in cached_tocs:
        return cached_tocs[parent_key]
    children = item.get("children") or []
    for child in children:
        if isinstance(child, dict):
            c_key = child.get("item_key") or child.get("key")
            if c_key and c_key in cached_tocs:
                return cached_tocs[c_key]
    return None


def _get_applicable_cached_tocs(
    active_items: list[dict[str, Any]] | None,
    cached_tocs: dict[str, Any] | None,
    max_items: int = MAX_INJECTED_TOC_ITEMS,
) -> list[str]:
    """Return formatted cached TOC outlines only if 100% of active items are cached.

    If active items are empty (whole library search), or if ANY active item
    lacks a cached TOC, returns an empty list to prevent asymmetric cognitive bias.
    """
    if not active_items or not isinstance(active_items, list) or not cached_tocs:
        return []

    if len(active_items) > max_items:
        return []

    # Complete Active Coverage Gate: Every active item must be in cached_tocs
    for item in active_items:
        if not _is_item_cached(item, cached_tocs):
            return []

    outlines: list[str] = []
    seen_texts: set[str] = set()
    for item in active_items:
        entry = _resolve_cached_entry(item, cached_tocs)
        if not entry or not entry.get("outline_text"):
            return []
        text = entry["outline_text"]
        if text not in seen_texts:
            seen_texts.add(text)
            outlines.append(text)

    return outlines


def _has_context_continuity(prior_active_items: Any, current_active_items: Any) -> bool:
    """Return True if prior turn and current turn share active context scope."""
    p_keys = {
        item.get("item_key") or item.get("key")
        for item in (prior_active_items or [])
        if isinstance(item, dict) and (item.get("item_key") or item.get("key"))
    }
    c_keys = {
        item.get("item_key") or item.get("key")
        for item in (current_active_items or [])
        if isinstance(item, dict) and (item.get("item_key") or item.get("key"))
    }
    # Both active: must share at least one paper
    if p_keys and c_keys:
        return bool(p_keys & c_keys)
    # Both whole library (empty active selection): continuous search thread
    if not p_keys and not c_keys:
        return True
    # Scope changed between single-paper and whole library: no continuity
    return False


@node(name="search_library_pipeline_node", rerun_on_resume=True)
async def search_library_pipeline_node(
    ctx: Context,
    node_input: str = "",
) -> str:
    """Dynamic workflow function orchestrating autonomous search, reranking, and synthesis.

    Wraps ``_run_search_pipeline`` in a try/finally so the node always closes
    its isolation scope (``search_library_pipeline:{fc_id}``) before returning, whether
    it succeeds ("Task completed.") or raises ("Task failed."). Closing the
    scope prevents ADK's Runner from treating the finished pipeline as an
    unclosed in-flight task agent and hijacking subsequent user turns.
    """
    _ensure_isolation_scope(ctx)

    completed = False
    interrupted = False
    try:
        response_text = await _run_search_pipeline(ctx, node_input)
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


search_pipeline_node = search_library_pipeline_node


async def _run_search_pipeline(ctx: Context, node_input: str) -> str:
    """Core search-pipeline body: subagent resolution, retrieval, rerank, and synthesis."""
    state = ctx.state
    snapshot = state.get(HITL_SNAPSHOT)
    if snapshot and snapshot.get("search_query"):
        search_query = snapshot["search_query"]
    else:
        raw_query = node_input or state.get(TEMP_SEARCH_QUERY, "")
        # Sanitize search query: strip any accidental numeric citation brackets ([1], [2], [1, 2], etc.)
        # that the coordinator may have copied from conversation history into its delegated query.
        search_query = re.sub(
            r"\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】",
            "",
            raw_query,
        ).strip()
    state[TEMP_SEARCH_QUERY] = search_query

    cfg = load_zocrates_config()
    h_cfg = cfg.get("search_harness") or {}
    hitl_enabled = bool(h_cfg.get("hitl_enabled", True))
    max_extensions = int(h_cfg.get("max_extension_count", 2))
    budget_increment = int(h_cfg.get("budget_extension_increment", 5))
    word_increment = int(h_cfg.get("word_ceiling_extension_increment", 3750))
    chunk_increment = int(h_cfg.get("rerank_chunks_extension_increment", 5))

    completed_extensions: list[int] = (
        list(snapshot.get("completed_extensions", []))
        if (snapshot and isinstance(snapshot, dict))
        else []
    )
    existing_interrupt_ids = _get_pipeline_interrupt_ids(ctx)

    if snapshot:
        ceilings = snapshot.get("ceilings", {})
        if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is None:
            state[TEMP_RETRIEVAL_TOOL_BUDGET] = ceilings.get("budget", h_cfg.get("max_retrieval_tools", 10))
        if state.get(TEMP_WORD_CEILING) is None:
            state[TEMP_WORD_CEILING] = ceilings.get("word_ceiling", h_cfg.get("hard_word_ceiling", 7500))
        if state.get(TEMP_MAX_RERANK_CHUNKS) is None:
            state[TEMP_MAX_RERANK_CHUNKS] = ceilings.get("max_chunks", h_cfg.get("max_rerank_chunks", 15))

    if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is None:
        state[TEMP_RETRIEVAL_TOOL_BUDGET] = int(h_cfg.get("max_retrieval_tools", 10))
    if state.get(TEMP_WORD_CEILING) is None:
        state[TEMP_WORD_CEILING] = int(h_cfg.get("hard_word_ceiling", 7500))
    if state.get(TEMP_MAX_RERANK_CHUNKS) is None:
        state[TEMP_MAX_RERANK_CHUNKS] = int(h_cfg.get("max_rerank_chunks", 15))
    if state.get(TEMP_EXTENSION_COUNT) is None:
        state[TEMP_EXTENSION_COUNT] = 0

    # Clear invocation user_content so ADK single_turn mode does not prepend
    # the root conversational chat message or _SINGLE_TURN_NUDGE into subagent requests.
    # Subagents receive ONLY their dedicated node_input.
    if hasattr(ctx, "_invocation_context") and ctx._invocation_context is not None:
        ctx._invocation_context.user_content = None

    # Subagent resolution: resolve from parent coordinator's _subagents if available
    parent_agent = (
        getattr(ctx._invocation_context, "agent", None)
        if hasattr(ctx, "_invocation_context")
        else None
    )
    subagents = getattr(parent_agent, "_subagents", {}) if parent_agent else {}
    search_agent = subagents.get("library_search")
    synth_agent = subagents.get("library_synthesizer")

    # Fallback to coordinator agent subagents when resuming directly into search_library_pipeline_node
    if (search_agent is None or synth_agent is None) and getattr(parent_agent, "name", None) in (
        "search_library_pipeline_node",
    ):
        from zocrates.agents.app import coordinator_agent

        if coordinator_agent and hasattr(coordinator_agent, "_subagents"):
            coord_subagents = getattr(coordinator_agent, "_subagents", {})
            if search_agent is None:
                search_agent = coord_subagents.get("library_search")
            if synth_agent is None:
                synth_agent = coord_subagents.get("library_synthesizer")

    # Fallback for standalone node execution outside of coordinator
    if search_agent is None or synth_agent is None:
        logger.warning(
            "Subagents could not be resolved from coordinator invocation context. "
            "Falling back to default standalone subagent construction via get_llm_model()."
        )
        from zocrates.llm import get_llm_model

        default_model = get_llm_model()
        if search_agent is None:
            search_agent = create_library_search_agent(default_model)
        if synth_agent is None:
            synth_agent = create_library_synthesizer_agent(default_model)

    active_items_str = state.get(ACTIVE_CONTEXT_ITEMS_STR, "")
    active_items = state.get(ACTIVE_CONTEXT_ITEMS)
    if active_items and isinstance(active_items, list):
        search_active_context_str = format_active_items(active_items, include_keys=True)
    else:
        search_active_context_str = active_items_str

    cached_tocs = state.get(CACHED_TOCS) or {}
    max_injected_tocs = int(h_cfg.get("max_injected_toc_items", MAX_INJECTED_TOC_ITEMS))
    applicable_tocs = _get_applicable_cached_tocs(active_items, cached_tocs, max_items=max_injected_tocs)

    outline_block = ""
    if applicable_tocs:
        joined_outlines = "\n\n".join(applicable_tocs)
        outline_block = (
            f"## Pre-Retrieved Document Outline(s) for Active Items:\n"
            f"{joined_outlines}\n\n"
            f"*(Note: Table of Contents is already provided above for these active items. "
            f"Do NOT call `get_document_toc` for them. You can call `read_document` using your chosen asset IDs with either key.)*\n\n"
        )

    focal_snippets_str = state.get("focal_snippets_str") or ""
    snippet_block = ""
    if focal_snippets_str.strip():
        s = focal_snippets_str.strip()
        if "# Context: User-Selected Focal Snippets from Attachment" not in s:
            snippet_block = f"# Context: User-Selected Focal Snippets from Attachment\n{s}"
        else:
            snippet_block = s

    input_parts: list[str] = []
    if search_active_context_str and search_active_context_str.strip():
        input_parts.append(f"# Context: Active Items Selected in Zotero\n{search_active_context_str.strip()}")
    if outline_block.strip():
        input_parts.append(outline_block.strip())
    if snippet_block.strip():
        input_parts.append(snippet_block.strip())
    input_parts.append(f"# Target Search Goal\n{search_query}")
    search_node_input = "\n\n".join(input_parts)

    active_isolation_scope = _ensure_isolation_scope(ctx)

    # Skip search_initial on resume since it was executed in the initial invocation
    curr_inv_id = getattr(ctx, "invocation_id", None) or (
        getattr(ctx._invocation_context, "invocation_id", None)
        if hasattr(ctx, "_invocation_context") else None
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

    # Deep Research Extension Loop
    extension_count = 0
    while True:
        evidence = state.get(TEMP_RETRIEVED_EVIDENCE) or (snapshot.get("evidence") if snapshot else None) or []
        search_report = state.get(TEMP_SEARCH_REPORT) or (snapshot.get("search_report") if snapshot else None)
        if search_report is None:
            search_report = _auto_generate_completion_report(
                search_query=search_query,
                evidence=evidence,
            )

        total_words = sum(
            c.get("word_count") if c.get("word_count") is not None else len(c.get("text", "").split())
            for c in evidence
        )
        current_calls = int(state.get(TEMP_RETRIEVAL_TOOL_COUNT) or (snapshot.get("tool_count") if snapshot else 0) or 0)
        current_budget = int(state.get(TEMP_RETRIEVAL_TOOL_BUDGET, 10))
        current_word_ceiling = int(state.get(TEMP_WORD_CEILING, 7500))
        current_max_chunks = int(state.get(TEMP_MAX_RERANK_CHUNKS, 15))
        target_pass = extension_count + 1
        req_pass = state.get(TEMP_EXTENSION_REQUEST_PASS)
        if req_pass is not None:
            extension_requested = (req_pass == target_pass)
        else:
            extension_requested = (
                bool(state.get(TEMP_EXTENSION_REQUESTED, False))
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
                or (bool(gaps) and (current_calls >= current_budget or total_words >= int(0.8 * current_word_ceiling)))
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
        elif total_words >= int(0.8 * current_word_ceiling):
            trigger_reason = "word_ceiling_approached"
        else:
            trigger_reason = "unknown"

        agent_justification = (
            search_report.get("notes")
            if (trigger_reason == "agent_request" and search_report.get("source") == "subagent_request")
            else None
        )

        # Save snapshot before checkpoint
        state[HITL_SNAPSHOT] = {
            "search_query": search_query,
            "evidence": evidence,
            "search_report": search_report,
            "extension_count": extension_count,
            "tool_count": current_calls,
            "extension_requested": extension_requested,
            "isolation_scope": active_isolation_scope,
            "completed_extensions": list(completed_extensions),
            "ceilings": {
                "budget": current_budget,
                "word_ceiling": current_word_ceiling,
                "max_chunks": current_max_chunks,
            },
            "seen_chunk_ids": list(state.get(TEMP_SEEN_CHUNK_IDS, [])),
            "seen_assets": list(state.get(TEMP_SEEN_ASSETS, [])),
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
                "words_used": total_words,
                "word_ceiling": current_word_ceiling,
            },
            "agent_justification": agent_justification,
        }

        checkpoint_payload = {
            "type": "deep_research_extension",
            "extension_number": extension_count + 1,
            "max_extensions": max_extensions,
            "current_calls": current_calls,
            "current_words": total_words,
            "covered_summary": search_report.get("summary", ""),
            "missing_aspects": gaps,
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
                "words_used": total_words,
                "word_ceiling": current_word_ceiling,
            },
            "agent_justification": agent_justification,
        }

        decision = await ctx.run_node(
            search_budget_checkpoint,
            node_input=checkpoint_payload,
            run_id=f"checkpoint_{extension_count}",
            override_isolation_scope=active_isolation_scope,
        )
        action = decision.get("action") if isinstance(decision, dict) else str(decision or "")

        if action != "extend_budget":
            # User chose "synthesize" or dismissed -> proceed directly to synthesis
            break

        # User granted extension
        new_budget = current_budget + budget_increment
        new_word_ceiling = current_word_ceiling + word_increment
        new_max_chunks = current_max_chunks + chunk_increment
        new_extension_count = extension_count + 1

        state[TEMP_RETRIEVAL_TOOL_BUDGET] = new_budget
        state[TEMP_WORD_CEILING] = new_word_ceiling
        state[TEMP_MAX_RERANK_CHUNKS] = new_max_chunks
        state[TEMP_EXTENSION_COUNT] = new_extension_count

        guidance = (decision.get("guidance") or "").strip() if isinstance(decision, dict) else ""
        gaps_list = "\n".join(f"- {g}" for g in gaps) if gaps else "- Continue targeted literature retrieval"
        continuation_prompt = (
            f"# Deep Research Extension Granted (Extension {new_extension_count} of {max_extensions})\n"
            "The user granted additional retrieval tool calls and evidence capacity.\n\n"
            f"## Remaining Gaps to Retrieve:\n{gaps_list}\n"
        )
        if guidance:
            continuation_prompt += f"\n## User Steering Guidance:\n{guidance}\n"

        if new_extension_count not in completed_extensions:
            await ctx.run_node(
                search_agent,
                node_input=continuation_prompt,
                run_id=f"search_extension_{new_extension_count}",
                override_isolation_scope=active_isolation_scope,
            )
            completed_extensions.append(new_extension_count)

        # Full-dict snapshot reassignment to ensure ADK State commits changes
        state[HITL_SNAPSHOT] = {
            "search_query": search_query,
            "evidence": state.get(TEMP_RETRIEVED_EVIDENCE) or evidence,
            "search_report": state.get(TEMP_SEARCH_REPORT) or search_report,
            "extension_count": new_extension_count,
            "tool_count": int(state.get(TEMP_RETRIEVAL_TOOL_COUNT) or current_calls),
            "extension_requested": False,
            "isolation_scope": active_isolation_scope,
            "completed_extensions": list(completed_extensions),
            "ceilings": {
                "budget": new_budget,
                "word_ceiling": new_word_ceiling,
                "max_chunks": new_max_chunks,
            },
            "seen_chunk_ids": list(state.get(TEMP_SEEN_CHUNK_IDS, [])),
            "seen_assets": list(state.get(TEMP_SEEN_ASSETS, [])),
            "trigger_reason": trigger_reason,
            "trigger_details": {
                "tool_calls_used": current_calls,
                "tool_budget": current_budget,
                "words_used": total_words,
                "word_ceiling": current_word_ceiling,
            },
            "agent_justification": agent_justification,
        }
        extension_count = new_extension_count

    evidence = state.get(TEMP_RETRIEVED_EVIDENCE) or (snapshot.get("evidence") if snapshot else []) or []

    # Active items abstract fallback: if search concluded without accumulating chunks
    # but active items are selected, backfill their abstracts so the synthesizer is not starved of evidence.
    if not evidence and active_items and isinstance(active_items, list):
        import json
        from zocrates.agents.common.toolkit import _resolve_bundle_dir
        for act in active_items:
            if not isinstance(act, dict):
                continue
            act_key = act.get("key") or act.get("item_key")
            if not act_key:
                continue
            try:
                resolved = _resolve_bundle_dir(act_key)
                if resolved:
                    b_dir, r_key, _ = resolved
                    toc_json_path = b_dir / "toc.json"
                    if toc_json_path.exists():
                        with open(toc_json_path, encoding="utf-8") as f:
                            t_data = json.load(f)
                        raw_abs = (t_data.get("abstract") or "").strip()
                        if raw_abs and raw_abs != "No abstract or summary available." and t_data.get("abstract_source") != "none":
                            evidence.append({
                                "chunk_id": f"{r_key}:abstract",
                                "item_key": r_key,
                                "title": act.get("title") or t_data.get("title", ""),
                                "chunk_type": "text",
                                "heading": "Abstract",
                                "text": raw_abs,
                                "word_count": len(raw_abs.split()),
                                "retrieval_method": "direct_read",
                                "cosine_similarity": None,
                                "rrf_score": None,
                                "rerank_score": None,
                                "image_path": None,
                                "structured_content": None,
                                "authors": act.get("authors") or act.get("creators"),
                                "year": act.get("year"),
                            })
            except Exception as exc:
                logger.debug("Failed active item abstract fallback: %s", exc)
        if evidence:
            state[TEMP_RETRIEVED_EVIDENCE] = evidence

    search_report = state.get(TEMP_SEARCH_REPORT) or (snapshot.get("search_report") if snapshot else None)
    if search_report is None:
        search_report = _auto_generate_completion_report(
            search_query=search_query,
            evidence=evidence,
        )
        state[TEMP_SEARCH_REPORT] = search_report

    # Rerank evidence with active dynamic ceilings
    active_max_chunks = int(state.get(TEMP_MAX_RERANK_CHUNKS, 15))
    active_word_ceiling = int(state.get(TEMP_WORD_CEILING, 7500))
    evidence = filter_substantive_evidence(evidence)
    ranked_chunks = await rerank_evidence(
        search_query,
        evidence,
        max_chunks=active_max_chunks,
        word_ceiling=active_word_ceiling,
    )
    evidence_str = format_retrieved_evidence_str(ranked_chunks)

    if active_items and isinstance(active_items, list):
        clean_active = format_active_items(active_items, include_keys=False)
    elif active_items_str and active_items_str.strip():
        clean_active = re.sub(
            r"(\[(?:Attachment\s+|Note\s+)?Key:\s*[^\]]+\]|\*\*Item Key\*\*:\s*[^\n]+)\s*",
            "",
            active_items_str.strip(),
        )
    else:
        clean_active = ""

    # Synthesize grounded answer
    user_query = state.get(USER_PROMPT)
    if not user_query:
        # Session event inspection fallback
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
        "pipeline": "library_search",
        "user_query": user_query,
        "search_goal": search_query,
        "conclude_output": {
            "summary": search_report.get("summary", ""),
            "gaps": search_report.get("gaps", []),
            "notes": search_report.get("notes"),
        },
    }
    history = list(state.get(RESEARCH_TURN_HISTORY) or [])
    if history and history[-1].get("invocation_id") == curr_inv_id:
        history[-1] = research_entry
    else:
        history.append(research_entry)
    state[RESEARCH_TURN_HISTORY] = history[-5:]

    synth_parts: list[str] = []

    profile_str = str(state.get(USER_RESEARCH_PROFILE_STR) or "").strip()
    if profile_str:
        synth_parts.append(profile_str)

    prior_audit = state.get(RETRIEVED_EVIDENCE) or []
    if prior_audit and isinstance(prior_audit, list):
        last_turn = prior_audit[-1]
        prior_active = last_turn.get("active_context_items")
        if _has_context_continuity(prior_active, active_items):
            report = last_turn.get("search_report") or {}
            if report.get("source") not in ("auto_generated", "budget_exhaustion"):
                last_query = last_turn.get("search_query") or last_turn.get("user_query")
                last_summary = report.get("summary")
                if last_summary and last_query:
                    synth_parts.append(
                        f"# Prior Turn Context (Read-Only Reference)\n"
                        f"Previous Inquiry: \"{last_query}\"\n"
                        f"Previous Findings Summary: {last_summary}"
                    )

    synth_parts.append(f"# User Query\n{user_query}")
    if clean_active:
        synth_parts.append(f"# Context: Active Items Selected in Zotero\n{clean_active}")
    if focal_snippets_str.strip():
        clean_synth_snippets = strip_snippet_keys(focal_snippets_str).strip()
        if clean_synth_snippets:
            if "# Context: User-Selected Focal Snippets from Attachment" not in clean_synth_snippets:
                synth_parts.append(
                    f"# Context: User-Selected Focal Snippets from Attachment\n{clean_synth_snippets}"
                )
            else:
                synth_parts.append(clean_synth_snippets)
    synth_parts.append(f"# Target Search Goal\n{search_query}")
    synth_parts.append(f"# Retrieved Literature Evidence\n{evidence_str}")

    synth_prompt = "\n\n".join(synth_parts)

    try:
        from zocrates.config import (
            get_considered_sources_disclosure,
            get_references_section_enabled,
        )
        zotero_dir = resolve_zotero_data_dir()
        cfg = load_zocrates_config(zotero_dir)
        citations_enabled = bool(cfg.get("citations_enabled", True))
        references_enabled = bool(cfg.get("references_section_enabled", True))
        considered_disclosure = bool(cfg.get("considered_sources_disclosure", True))
        verification_enabled = bool(cfg.get("citation_verification_enabled", False))
        verification_threshold = float(cfg.get("citation_verification_threshold", 0.60))
    except Exception:
        zotero_dir = None
        cfg = {}
        citations_enabled = True
        references_enabled = True
        considered_disclosure = True
        verification_enabled = False
        verification_threshold = 0.60
    if hasattr(synth_agent, "instruction"):
        try:
            synth_agent.instruction = get_synthesizer_instruction(citations_enabled)
        except (AttributeError, TypeError):
            pass

    synth_response = await ctx.run_node(
        synth_agent,
        node_input=synth_prompt,
        use_as_output=True,
        override_isolation_scope=active_isolation_scope,
    )
    raw_synth_text = coerce_to_text(synth_response)
    raw_synth_text = _rewrite_relative_markdown_images(raw_synth_text, ranked_chunks, active_items)
    response_text = _clean_user_facing_synthesis(raw_synth_text)

    # Optional citation support verification via fast decision models
    support_probabilities: dict[int, list[float | None]] | dict[int, float | None] | None = None
    verification_telemetry: dict[str, Any] = {}
    if verification_enabled and ranked_chunks:
        try:
            from zocrates.agents.library_search.citation_verifier import verify_citations_support

            support_probabilities = await verify_citations_support(
                text=response_text,
                ranked_chunks=ranked_chunks,
                config=cfg,
                telemetry_out=verification_telemetry,
            )
        except Exception as ver_err:
            logger.warning("Error running citation verification in search pipeline: %s", ver_err)
            support_probabilities = None

    # Resolve citations & references in-process without out-of-band SQL writes
    citations: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    cited_chunk_ids: list[str] = []
    enriched_text: str | None = response_text

    if (citations_enabled or references_enabled) and ranked_chunks:
        try:
            from zocrates.agents.library_search.citation_resolver import resolve_citations_and_references

            resolved = resolve_citations_and_references(
                text=response_text,
                ranked_chunks=ranked_chunks,
                zotero_dir=zotero_dir,
                references_enabled=references_enabled,
                considered_disclosure=considered_disclosure,
                citations_enabled=citations_enabled,
                support_probabilities=support_probabilities,
                verification_threshold=verification_threshold,
            )
            response_text = resolved["enriched_text"]
            citations = resolved["citations"]
            references = resolved["references"]
            cited_chunk_ids = resolved["cited_chunk_ids"]
            enriched_text = response_text
        except Exception as res_err:
            logger.warning("Error resolving citations in search pipeline: %s", res_err)
            enriched_text = None

    # Append turn audit log to persistent session state (capped to MAX_AUDIT_HISTORY_TURNS)
    new_audit_entry = {
        "invocation_id": curr_inv_id,
        "user_query": user_query,
        "search_query": search_query,
        "active_context_items": list(active_items) if (active_items and isinstance(active_items, list)) else [],
        "evidence_chunks": ranked_chunks,
        "cited_chunk_ids": cited_chunk_ids,
        "citations": citations,
        "references": references,
        "search_report": search_report,
        "support_probabilities": support_probabilities,
        "citation_verification_telemetry": verification_telemetry or None,
        "enriched_text": enriched_text,
    }

    audit_history = list(state.get(RETRIEVED_EVIDENCE) or [])
    # Deduplicate on resume/retry: if last entry matches the same search_query and user_query, update it in-place
    if (
        audit_history
        and audit_history[-1].get("search_query") == search_query
        and audit_history[-1].get("user_query") == user_query
    ):
        audit_history[-1] = new_audit_entry
    else:
        audit_history.append(new_audit_entry)

    if len(audit_history) > MAX_AUDIT_HISTORY_TURNS:
        audit_history = audit_history[-MAX_AUDIT_HISTORY_TURNS:]

    state[RETRIEVED_EVIDENCE] = audit_history

    # Clean up ephemeral turn-level snapshot so it never leaks into subsequent turns
    state[HITL_SNAPSHOT] = None
    state[TEMP_EXTENSION_REQUESTED] = False
    state[TEMP_EXTENSION_REQUEST_PASS] = None
    state[TEMP_EXTENSION_COUNT] = None
    state[TEMP_RETRIEVAL_TOOL_BUDGET] = None
    state[TEMP_WORD_CEILING] = None
    state[TEMP_MAX_RERANK_CHUNKS] = None

    # Build a clean Markdown view with in-text parenthetical author-year citations
    # and clean Markdown bibliography for the coordinator agent.
    clean_coordinator_text = build_coordinator_view(
        text=response_text,
        citations=citations,
        references=references,
    )

    return clean_coordinator_text
