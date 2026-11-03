"""Tools for the Online Literature Discovery Subagent."""

import logging
import re
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field

from zocrates.agents.common.state_keys import (
    CANDIDATE_PAPERS,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_EXTENSION_REQUEST_PASS,
    TEMP_ONLINE_CANDIDATES,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_SEARCH_REPORT,
)
from zocrates.discovery.engine import DiscoveryEngine, get_default_discovery_engine
from zocrates.discovery.models import CandidatePaper

logger = logging.getLogger(__name__)

INITIAL_ONLINE_TOOL_BUDGET: int = 8
MAX_ONLINE_EXTENSIONS: int = 2
ONLINE_BUDGET_EXTENSION_INCREMENT: int = 5


class CandidateRecommendation(BaseModel):
    """Structured paper recommendation with rationale for import."""

    candidate_id: str = Field(
        description="Candidate ID as shown in the search results (e.g. 'https://openalex.org/W...' or short 'W...')"
    )
    rationale: str = Field(
        description="Strictly 1-2 concise sentences (MAX 35 words). State ONLY the core takeaway or mechanism answering the research inquiry. Do NOT write paragraphs, bullet points, or lengthy summaries."
    )


def _resolve_candidate_from_pool(
    cid_raw: Any,
    pool: dict[str, Any],
) -> CandidatePaper | None:
    """Resolve a candidate identifier to a CandidatePaper from the pool.

    Supports multi-tier resolution:
    1. Direct exact key match (`cid in pool`).
    2. Short ID match: `cid` matches the suffix of a pool key (e.g., 'W3128491823' matches 'https://openalex.org/W3128491823').
    3. Ordinal / Index match: if `cid` is 'c1', '1', 1, etc., extract integer N and map to N-1 index of pool values.
    4. Direct identifier match: `cid` matches `cand.doi` or `cand.arxiv_id`.
    """
    if cid_raw is None or not pool:
        return None

    def _to_candidate(val: Any) -> CandidatePaper | None:
        """Coerce a candidate pool entry into a CandidatePaper instance.

        Args:
            val: CandidatePaper instance or dictionary representation.

        Returns:
            CandidatePaper instance, or None if conversion fails.
        """
        if isinstance(val, CandidatePaper):
            return val
        if isinstance(val, dict):
            try:
                return CandidatePaper.from_dict(val)
            except Exception:
                return None
        return None

    # Tier 1: Direct exact key match
    if cid_raw in pool:
        cand = _to_candidate(pool[cid_raw])
        if cand is not None:
            return cand

    cid_str = str(cid_raw).strip()
    if not cid_str:
        return None

    if cid_str in pool:
        cand = _to_candidate(pool[cid_str])
        if cand is not None:
            return cand

    # Tier 2: Short ID match (suffix of a pool key or openalex_id)
    cid_lower = cid_str.lower()
    for key, val in pool.items():
        key_str = str(key).strip()
        key_suffix = key_str.rstrip("/").split("/")[-1].lower()
        if key_suffix == cid_lower or key_str.lower().endswith("/" + cid_lower):
            cand = _to_candidate(val)
            if cand is not None:
                return cand
        cand = _to_candidate(val)
        if cand is not None and cand.openalex_id:
            oa_suffix = cand.openalex_id.rstrip("/").split("/")[-1].lower()
            if oa_suffix == cid_lower or cand.openalex_id.lower() == cid_lower:
                return cand

    # Tier 3: Ordinal / Index match (e.g. 1, "1", "c1", "C1", "candidate 1")
    ordinal_num: int | None = None
    if isinstance(cid_raw, int):
        ordinal_num = cid_raw
    elif cid_str.isdigit():
        ordinal_num = int(cid_str)
    else:
        m = re.match(r"^(?:c|candidate)[\s_-]*(\d+)$", cid_str, re.IGNORECASE)
        if m:
            ordinal_num = int(m.group(1))

    if ordinal_num is not None:
        pool_values = list(pool.values())
        if 1 <= ordinal_num <= len(pool_values):
            cand = _to_candidate(pool_values[ordinal_num - 1])
            if cand is not None:
                return cand

    # Tier 4: Direct identifier match (DOI or arXiv ID)
    norm_cid = cid_lower
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "doi:",
        "https://arxiv.org/abs/",
        "https://arxiv.org/pdf/",
        "arxiv:",
    ):
        if norm_cid.startswith(prefix):
            norm_cid = norm_cid[len(prefix) :]
            break

    for val in pool.values():
        cand = _to_candidate(val)
        if cand is None:
            continue
        if cand.doi:
            cand_doi = cand.doi.lower().strip()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if cand_doi.startswith(prefix):
                    cand_doi = cand_doi[len(prefix) :]
                    break
            if cand_doi == norm_cid or cand_doi == cid_lower:
                return cand
        if cand.arxiv_id:
            cand_arxiv = cand.arxiv_id.lower().strip()
            for prefix in (
                "https://arxiv.org/abs/",
                "https://arxiv.org/pdf/",
                "arxiv:",
            ):
                if cand_arxiv.startswith(prefix):
                    cand_arxiv = cand_arxiv[len(prefix) :]
                    break
            if cand_arxiv == norm_cid or cand_arxiv == cid_lower:
                return cand

    return None


def _check_and_increment_online_budget(
    tool_context: ToolContext | None,
) -> tuple[bool, str | None, str | None]:
    """Check and update tool call budget for online retrieval."""
    if tool_context is None:
        return True, None, None

    state = tool_context.state
    raw_budget = (
        state.get(TEMP_RETRIEVAL_TOOL_BUDGET)
        if state.get(TEMP_RETRIEVAL_TOOL_BUDGET) is not None
        else state.get("temp_retrieval_tool_budget")
    )
    budget = int(raw_budget) if raw_budget is not None else INITIAL_ONLINE_TOOL_BUDGET

    raw_count = (
        state.get(TEMP_RETRIEVAL_TOOL_COUNT)
        if state.get(TEMP_RETRIEVAL_TOOL_COUNT) is not None
        else state.get("temp_retrieval_tool_count")
    )
    count = int(raw_count) if raw_count is not None else 0

    extension_count = int(
        state.get(TEMP_EXTENSION_COUNT)
        if state.get(TEMP_EXTENSION_COUNT) is not None
        else state.get("temp_extension_count", 0)
    )
    can_extend = extension_count < MAX_ONLINE_EXTENSIONS

    if count >= budget:
        action_msg = (
            "Call `request_search_extension` to request an extension, or `conclude_online_search` to finish."
            if can_extend
            else "Call `conclude_online_search` immediately with your recommendations to finish."
        )
        return (
            False,
            f"Retrieval tool budget reached ({budget}/{budget}). You cannot call more search tools. {action_msg}",
            None,
        )

    new_count = count + 1
    state[TEMP_RETRIEVAL_TOOL_COUNT] = new_count
    state["temp_retrieval_tool_count"] = new_count

    notice: str | None = None
    if new_count == budget:
        action_msg = (
            "Call `request_search_extension` to request an extension, or `conclude_online_search` to finish."
            if can_extend
            else "You must call `conclude_online_search` now to conclude retrieval."
        )
        notice = f"[Notice: Retrieval tool budget exhausted ({budget}/{budget}). {action_msg}]"
    elif new_count == (budget - 1):
        notice = (
            f"[Notice: {new_count}/{budget} retrieval tool calls used (1 call remaining). "
            f"Plan your final query, request an extension via `request_search_extension`, or prepare to call `conclude_online_search`.]"
        )
    elif new_count >= int(budget / 2):
        notice = (
            f"[Notice: {new_count}/{budget} retrieval tool calls used. "
            f"Continue your search, being mindful of your remaining retrieval budget.]"
        )

    return True, None, notice


async def search_openalex(
    query: str,
    year_from: int | None = None,
    year_to: int | None = None,
    is_oa_only: bool = False,
    tool_context: ToolContext | None = None,
) -> str:
    """Search OpenAlex for external scholarly papers and preprints.

    Automatically excludes papers already in your local Zotero library and retrieves
    essential metadata, abstracts, and open-access PDF links.

    Args:
        query: Academic search query, keywords, paper title, or exact DOI/arXiv identifier.
        year_from: Optional starting publication year (e.g. 2023).
        year_to: Optional ending publication year (e.g. 2025).
        is_oa_only: When True, restricts results strictly to verified open-access works.
        tool_context: ADK ToolContext.

    Returns:
        Structured summary of discovered novel candidate papers.
    """
    allowed, err_msg, notice = _check_and_increment_online_budget(tool_context)
    if not allowed:
        return err_msg or "Search budget exhausted."

    engine: DiscoveryEngine | None = None
    if tool_context is not None and tool_context.state:
        raw_engine = tool_context.state.get("discovery_engine")
        if hasattr(raw_engine, "search") and callable(getattr(raw_engine, "search")):
            engine = raw_engine
        elif hasattr(tool_context.state, "pop"):
            tool_context.state.pop("discovery_engine", None)
        elif raw_engine is not None:
            tool_context.state["discovery_engine"] = None
    if engine is None:
        engine = get_default_discovery_engine()

    try:
        results: list[CandidatePaper] = await engine.search(
            query=query,
            limit=10,
            year_from=year_from,
            year_to=year_to,
            is_oa_only=is_oa_only,
        )
    except Exception as e:
        logger.warning("search_openalex failed for query %r: %s", query, e)
        return f"Error executing search for query '{query}': {e}"

    # Update candidate pool in state
    if tool_context is not None and tool_context.state is not None:
        state = tool_context.state
        pool = (
            state.get(TEMP_ONLINE_CANDIDATES)
            if state.get(TEMP_ONLINE_CANDIDATES) is not None
            else state.get("temp_online_candidates")
        )
        if not isinstance(pool, dict):
            pool = {}
        for c in results:
            pool[c.candidate_id] = c.to_dict() if hasattr(c, "to_dict") else c
        state[TEMP_ONLINE_CANDIDATES] = pool
        state["temp_online_candidates"] = pool

    if not results:
        msg = (
            f"No novel candidate papers found matching query: '{query}'. "
            "Try refining, broadening, or using alternative technical terminology."
        )
        if notice:
            msg += f"\n\n{notice}"
        return msg

    lines = [f"Found {len(results)} novel candidate paper(s) matching query '{query}':\n"]
    for i, c in enumerate(results, 1):
        authors_str = ", ".join(c.authors[:3]) + (" et al." if len(c.authors) > 3 else "")
        oa_str = "Yes" if c.is_oa else "No"
        venue_str = f" | Venue: {c.venue}" if c.venue else ""
        lines.append(
            f"{i}. [Candidate ID: {c.candidate_id}] {c.title} ({c.year or 'n.d.'})\n"
            f"   Authors: {authors_str or 'Unknown'}{venue_str} | Citations: {c.citation_count} | OA: {oa_str}\n"
            f"   Topic: {c.primary_topic or 'N/A'}"
        )
        abstract_txt = c.abstract or c.tldr
        if abstract_txt:
            snippet = abstract_txt[:300].strip() + ("..." if len(abstract_txt) > 300 else "")
            lines.append(f"   Abstract: {snippet}")
        elif c.keywords:
            lines.append(f"   Keywords: {', '.join(c.keywords[:5])}")
        lines.append("")

    if notice:
        lines.append(notice)

    return "\n".join(lines).strip()


def conclude_online_search(
    recommendations: list[CandidateRecommendation],
    summary: str | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Conclude the online literature retrieval phase with selected candidate papers.

    Call this tool when novel, high-impact candidate papers addressing the user's
    research inquiry have been identified.

    Args:
        recommendations: Structured list of recommendations pairing candidate_id and rationale.
        summary: Brief summary of the literature discovered and why these papers were selected.
        tool_context: ADK ToolContext.

    Returns:
        Confirmation message concluding the retrieval phase.
    """
    selected_candidates: list[CandidatePaper] = []
    if tool_context is not None and tool_context.state is not None:
        state = tool_context.state
        pool = (
            state.get(TEMP_ONLINE_CANDIDATES)
            if state.get(TEMP_ONLINE_CANDIDATES) is not None
            else state.get("temp_online_candidates")
        ) or {}
        if not isinstance(pool, dict):
            pool = {}

        seen_ids: set[str] = set()
        for rec in recommendations or []:
            if isinstance(rec, dict):
                cid_raw = rec.get("candidate_id")
                rat = str(rec.get("rationale", "") or "").strip()
            elif hasattr(rec, "candidate_id"):
                cid_raw = getattr(rec, "candidate_id", None)
                rat = str(getattr(rec, "rationale", "") or "").strip()
            else:
                continue

            if cid_raw is None or cid_raw == "":
                continue

            cand_obj = _resolve_candidate_from_pool(cid_raw, pool)
            if cand_obj is not None:
                if cand_obj.candidate_id in seen_ids:
                    continue
                seen_ids.add(cand_obj.candidate_id)
                cand = (
                    CandidatePaper.from_dict(cand_obj.to_dict())
                    if hasattr(cand_obj, "to_dict")
                    else CandidatePaper.from_dict(cand_obj)
                )
                if rat:
                    cand.tldr = rat
                selected_candidates.append(cand)
            else:
                logger.warning("Candidate ID %r not found in online candidate pool.", cid_raw)

        state["selected_candidates"] = selected_candidates
        state[CANDIDATE_PAPERS] = [c.to_dict() for c in selected_candidates]

        report = {
            "summary": summary or f"Selected {len(selected_candidates)} candidate papers.",
            "gaps": [],
            "notes": summary or "",
            "source": "conclude_online_search",
        }
        state[TEMP_SEARCH_REPORT] = report
        state["temp_online_search_report"] = report

        if tool_context.actions:
            tool_context.actions.skip_summarization = True

    count = (
        len(selected_candidates)
        if (tool_context and tool_context.state is not None)
        else len(recommendations or [])
    )
    return (
        f"Online literature search concluded with {count} candidate paper(s) selected. "
        "The Literature Synthesizer will now prepare the recommendations."
    )


def request_search_extension(
    rationale: str,
    covered_summary: str | None = None,
    missing_aspects: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Request permission from the user to extend the online literature retrieval budget.

    Call this tool when discovering adjacent subfields or preprints that warrant deeper
    literature exploration beyond the initial tool budget.

    Args:
        rationale: Clear explanation of why additional search queries or deeper prospecting are needed.
        covered_summary: Optional summary of candidates found so far.
        missing_aspects: Optional list of missing topics or subfields to explore.
        tool_context: ADK ToolContext.

    Returns:
        Confirmation message confirming extension request.
    """
    if tool_context is not None and tool_context.state is not None:
        state = tool_context.state
        state[TEMP_EXTENSION_REQUESTED] = True
        state["temp_extension_requested"] = True
        state["extension_rationale"] = rationale

        ext_num = int(
            state.get(TEMP_EXTENSION_COUNT)
            if state.get(TEMP_EXTENSION_COUNT) is not None
            else state.get("temp_extension_count", 0)
        ) + 1
        state[TEMP_EXTENSION_REQUEST_PASS] = ext_num

        report = {
            "summary": covered_summary or "Preliminary literature discovery pass",
            "gaps": missing_aspects or [rationale],
            "notes": rationale,
            "source": "subagent_request",
        }
        state[TEMP_SEARCH_REPORT] = report
        state["temp_online_search_report"] = report

        if tool_context.actions:
            tool_context.actions.skip_summarization = True

    return "Online search extension requested. Concluding retrieval turn for user confirmation."
