"""ADK FunctionTool adapters for Zocrates core toolkit.

Binds pure business logic functions from `toolkit.py` to Google ADK session
and invocation state (`ToolContext`), handling:
- Scoping to active context items from Zotero UI
- Evidence accumulation in `temp:retrieved_evidence`
- Cross-tool deduplication in `temp:seen_chunk_ids`
- Range-Aware Pre-Exclusion
- Intra-tool asset deduplication in `temp:seen_assets`
"""

import contextlib
import logging
import re
import sqlite3
import time
from typing import Any

from google.adk.tools import FunctionTool, ToolContext

from zocrates.agents.common.config import MAX_UNIQUE_CACHED_TOCS
from zocrates.agents.common.reranker import HARD_WORD_CEILING
from zocrates.agents.common.state_keys import (
    ACTIVE_CONTEXT_ITEMS,
    CACHED_TOCS,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_EXTENSION_REQUEST_PASS,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEARCH_REPORT,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
    TEMP_SEEN_TOC_ITEM_KEYS,
    TEMP_WORD_CEILING,
)
from zocrates.agents.common.toolkit import (
    _prune_cached_tocs,
    _resolve_bundle_dir,
    _validate_item_key,
    extract_compact_toc_outline,
    get_document_toc_core,
    get_related_items_core,
    normalize_asset_ids,
    read_document_core,
    search_corpus_core,
)

logger = logging.getLogger(__name__)

MAX_RETRIEVAL_TOOLS_PER_RUN: int = 10
MAX_ABSOLUTE_RETRIEVAL_LIMIT: int = 20


def _check_and_increment_tool_budget(
    tool_context: ToolContext | None,
) -> tuple[bool, str | None, str | None]:
    """Check whether a retrieval tool call is permitted under budget and word ceilings.

    Returns:
        tuple of (allowed: bool, error_message: str | None, budget_notice: str | None)
    """
    if tool_context is None:
        return True, None, None

    budget = int(tool_context.state.get(TEMP_RETRIEVAL_TOOL_BUDGET, MAX_RETRIEVAL_TOOLS_PER_RUN))
    word_ceiling = int(tool_context.state.get(TEMP_WORD_CEILING, HARD_WORD_CEILING))
    extension_count = int(tool_context.state.get(TEMP_EXTENSION_COUNT, 0))

    hitl_enabled = True
    max_extensions = 2
    trigger_pct = 0.8
    try:
        from zocrates.config import get_search_harness_config
        h_cfg = get_search_harness_config()
        hitl_enabled = bool(h_cfg.get("hitl_enabled", True))
        max_extensions = int(h_cfg.get("max_extension_count", 2))
        trigger_pct = float(h_cfg.get("word_ceiling_hitl_trigger_percent", 80)) / 100.0
    except Exception:
        pass

    can_extend = hitl_enabled and (extension_count < max_extensions)

    evidence = tool_context.state.get(TEMP_RETRIEVED_EVIDENCE, [])
    total_words = sum(
        c.get("word_count") if c.get("word_count") is not None else len(c.get("text", "").split())
        for c in evidence
    )
    if total_words >= word_ceiling:
        _ensure_preliminary_report(tool_context)
        action_msg = (
            "Call `request_search_extension` to request an extension, or `conclude_search` to finish."
            if can_extend
            else "Call `conclude_search` immediately to conclude retrieval."
        )
        return (
            False,
            (
                f"Word ceiling reached (~{total_words}/{word_ceiling} words). "
                f"You cannot call more retrieval tools. {action_msg}"
            ),
            None,
        )

    count = int(tool_context.state.get(TEMP_RETRIEVAL_TOOL_COUNT, 0))
    if count >= budget:
        _ensure_preliminary_report(tool_context)
        action_msg = (
            "Call `request_search_extension` to request an extension, or `conclude_search` to finish."
            if can_extend
            else "Call `conclude_search` immediately to conclude retrieval."
        )
        return (
            False,
            (
                f"Retrieval tool budget reached ({budget}/"
                f"{budget}). You cannot call more retrieval tools. {action_msg}"
            ),
            None,
        )

    new_count = count + 1
    tool_context.state[TEMP_RETRIEVAL_TOOL_COUNT] = new_count

    notice: str | None = None
    if new_count == budget:
        _ensure_preliminary_report(tool_context)
        action_msg = (
            "Call `request_search_extension` to request an extension, or `conclude_search` to conclude."
            if can_extend
            else "You must call `conclude_search` now to conclude retrieval."
        )
        notice = (
            f"[Notice: Retrieval tool budget exhausted ({budget}/{budget}). {action_msg}]"
        )
    elif new_count == (budget - 1):
        notice = (
            f"[Notice: {new_count}/{budget} retrieval tool calls used (1 call remaining). "
            f"Plan your final retrieval, request an extension via `request_search_extension`, or prepare to call `conclude_search`.]"
        )
    elif total_words >= int(trigger_pct * word_ceiling):
        notice = (
            f"[Notice: Evidence volume approaching ceiling (~{total_words}/{word_ceiling} words). "
            f"If crucial aspects remain uncovered, consider calling request_search_extension or targeting only essential sections.]"
        )
    elif new_count >= int(budget / 2):
        notice = (
            f"[Notice: {new_count}/{budget} retrieval tool calls used. "
            f"Continue your search, being mindful of your remaining retrieval budget as you proceed.]"
        )

    return True, None, notice


def _ensure_preliminary_report(tool_context: ToolContext | None, force: bool = False) -> None:
    """Auto-generate preliminary report if not already set (or forced upon tool completion) when budget or word ceiling is exhausted."""
    if tool_context is None:
        return
    if not force and tool_context.state.get(TEMP_SEARCH_REPORT) is not None:
        return

    evidence = tool_context.state.get(TEMP_RETRIEVED_EVIDENCE, [])
    total_words = sum(
        c.get("word_count") if c.get("word_count") is not None else len(c.get("text", "").split())
        for c in evidence
    )
    if not evidence:
        report = {
            "summary": f"Retrieval tool limit reached ({MAX_RETRIEVAL_TOOLS_PER_RUN}/{MAX_RETRIEVAL_TOOLS_PER_RUN}) with 0 chunks retrieved.",
            "notes": "No evidence was retrieved.",
            "gaps": [],
            "source": "budget_exhaustion",
        }
    else:
        unique_headings: list[str] = []
        seen = set()
        for c in evidence:
            h = c.get("heading")
            if h and h not in seen:
                seen.add(h)
                unique_headings.append(h)
        chunk_types = sorted(list({c.get("chunk_type", "text") for c in evidence}))
        top_headings = unique_headings[:5]
        count = tool_context.state.get(TEMP_RETRIEVAL_TOOL_COUNT, 0)
        report = {
            "summary": (
                f"Retrieved {len(evidence)} chunks ({', '.join(chunk_types)}) "
                f"covering: {', '.join(top_headings) if top_headings else 'general content'} (~{total_words} words)."
            ),
            "notes": "Concluded via retrieval budget limit.",
            "gaps": [f"Unchecked literature beyond budget limit (~{total_words} words, {count} calls)"],
            "source": "budget_exhaustion",
        }
    tool_context.state[TEMP_SEARCH_REPORT] = report


def conclude_search(
    summary: str,
    notes: str | None = None,
    gaps: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Conclude the retrieval phase and hand off accumulated evidence to the synthesizer.

    Call this tool as soon as you have collected the necessary evidence, or immediately
    upon exhausting your retrieval tool budget. Do NOT write final answers or syntheses.

    Args:
        summary: 1-2 sentence overview of the evidence gathered.
        notes: Optional observations regarding caveats or overall synthesis guidance.
        gaps: Optional list of missing facets or literature gaps.
            If there are no gaps, leave this parameter unspecified.

    Returns:
        Confirmation dictionary indicating search has concluded.
    """
    if isinstance(gaps, str):
        gap_list = [g.strip() for g in re.split(r"[,;\n]+", gaps) if g.strip()]
    elif isinstance(gaps, (list, tuple, set)):
        gap_list = [str(g).strip() for g in gaps if g]
    else:
        gap_list = []

    report = {
        "summary": str(summary or "").strip(),
        "notes": str(notes or "").strip() if notes else None,
        "gaps": gap_list,
        "source": "subagent",
    }
    if tool_context is not None:
        tool_context.state[TEMP_SEARCH_REPORT] = report
        if getattr(tool_context, "actions", None) is not None:
            tool_context.actions.skip_summarization = True

    return {
        "status": "search_concluded",
        "message": "Retrieval phase concluded. Accumulated evidence will now be synthesized.",
        "report": report,
    }


def search_corpus(
    query: str,
    item_key: str | list[str] | None = "active",
    chunk_type: str | list[str] | None = None,
    limit: int = 10,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Hybrid (dense vector + FTS5 BM25) search over indexed library chunks with compound pre-filtering.

    Args:
        query: The search query string. If empty, returns immediately.
        item_key: Scope of the search:
            - "active" (default): searches active context items selected in Zotero (or entire library if none selected).
            - None, "all", or "*": searches across the ENTIRE library (unfiltered).
            - Specific item key(s) (e.g. "HCZLU22L" or ["HCZLU22L", "M5Z7SFWM"]): filters strictly to those items.
        chunk_type: Chunk type filter ('frontmatter', 'figure', 'table', 'equation', 'code', 'text').
            Omit, None, or "all" to search across all chunk types.
        limit: Maximum number of chunks to return (default 10).

    Returns:
        Dictionary with 'status' and list of matching 'results'.
    """
    if not query or not query.strip():
        return {
            "status": "error",
            "message": "Query string cannot be empty.",
            "results": [],
        }

    allowed, err_msg, notice = _check_and_increment_tool_budget(tool_context)
    if not allowed:
        return {"status": "budget_exhausted", "error": err_msg}

    filter_keys = item_key
    seen_chunk_ids: list[str] | None = None

    if tool_context is not None:
        # Resolve active selection scope only when explicitly requested via "active", "selected", or "context"
        if isinstance(filter_keys, str) and filter_keys.strip().lower() in ("active", "selected", "context"):
            active_items = tool_context.state.get(ACTIVE_CONTEXT_ITEMS)
            if active_items and isinstance(active_items, list):
                extracted = [
                    item.get("item_key") or item.get("key")
                    for item in active_items
                    if isinstance(item, dict) and (item.get("item_key") or item.get("key"))
                ]
                filter_keys = extracted if extracted else None
            else:
                filter_keys = None

        raw_seen = tool_context.state.get(TEMP_SEEN_CHUNK_IDS, [])
        if isinstance(raw_seen, (list, set)):
            seen_chunk_ids = list(raw_seen)

    res = search_corpus_core(
        query=query,
        item_key=filter_keys,
        chunk_type=chunk_type,
        limit=limit,
        exclude_chunk_ids=seen_chunk_ids,
    )

    if res.get("status") == "success" and tool_context is not None:
        results = res.get("results", [])
        new_ids = [r["chunk_id"] for r in results if "chunk_id" in r]

        # Update temp:seen_chunk_ids
        current_seen = list(tool_context.state.get(TEMP_SEEN_CHUNK_IDS, []))
        seen_set = set(current_seen)
        seen_set.update(new_ids)
        tool_context.state[TEMP_SEEN_CHUNK_IDS] = list(seen_set)

        # Update temp:retrieved_evidence
        current_evidence = list(tool_context.state.get(TEMP_RETRIEVED_EVIDENCE, []))
        for r in results:
            chunk_entry: dict[str, Any] = {
                "chunk_id": r["chunk_id"],
                "item_key": r["item_key"],
                "title": r.get("title", ""),
                "chunk_type": r.get("chunk_type", "text"),
                "heading": r.get("heading", ""),
                "text": r.get("text", ""),
                "word_count": r.get("word_count", 0),
                "retrieval_method": "hybrid",
                "cosine_similarity": r.get("cosine_similarity"),
                "rrf_score": r.get("rrf_score"),
                "rerank_score": None,
                "image_path": r.get("image_path"),
                "structured_content": None,
            }
            if "authors" in r:
                chunk_entry["authors"] = r["authors"]
            if "year" in r:
                chunk_entry["year"] = r["year"]
            current_evidence.append(chunk_entry)
        tool_context.state[TEMP_RETRIEVED_EVIDENCE] = current_evidence

        total_words = sum(c.get("word_count", 0) for c in current_evidence)
        res["retrieval_status"] = (
            f"[Retrieval status: {len(new_ids)} new chunks retrieved, "
            f"~{total_words} words accumulated in evidence set]"
        )

    if notice and tool_context is not None:
        _ensure_preliminary_report(tool_context, force=True)
        res["budget_notice"] = notice

    return res


def get_document_toc(
    item_key: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve the Title, Abstract, Table of Contents, Figures, and Tables for a given item.

    Args:
        item_key: Unique Zotero item key.

    Returns:
        Dictionary with 'status', 'item_key', and 'toc_content'.
    """
    allowed, err_msg, notice = _check_and_increment_tool_budget(tool_context)
    if not allowed:
        return {"status": "budget_exhausted", "error": err_msg}

    # Cycle suppression check for item_key before disk reading
    if tool_context is not None:
        seen_tocs = tool_context.state.get(TEMP_SEEN_TOC_ITEM_KEYS, [])
        if item_key in seen_tocs:
            res: dict[str, Any] = {
                "status": "already_inspected",
                "item_key": item_key,
                "message": (
                    f"TOC for {item_key} was already retrieved in this turn. "
                    "Refer to prior context or use read_document for specific sections."
                ),
            }
            if notice:
                _ensure_preliminary_report(tool_context, force=True)
                res["budget_notice"] = notice
            return res

    # Resolve bundle_dir and canonical resolved_key
    resolved_key = item_key
    bundle_dir = None
    try:
        resolved = _resolve_bundle_dir(item_key)
        if resolved:
            bundle_dir, r_key, _ = resolved
            resolved_key = r_key or resolved_key
    except Exception as exc:
        logger.debug("Failed to resolve bundle dir for TOC: %s", exc)

    # Check cached_tocs liveness: evict if item was deleted in Zotero
    if tool_context is not None:
        # ``or {}`` guards against an explicit None persisted in session state
        # (``.get(key, {})`` returns None when the key exists with a None value).
        cached_tocs = dict(tool_context.state.get(CACHED_TOCS) or {})
        if item_key in cached_tocs or resolved_key in cached_tocs:
            try:
                from zocrates.agents.common.toolkit import get_syncer
                syncer = get_syncer()
                is_active = syncer.db.is_item_active(resolved_key) if (syncer and syncer.db) else True
            except Exception:
                is_active = True
            if not is_active:
                cached_tocs.pop(item_key, None)
                cached_tocs.pop(resolved_key, None)
                tool_context.state[CACHED_TOCS] = cached_tocs
                return {
                    "status": "error",
                    "item_key": item_key,
                    "error": f"Document '{item_key}' has been deleted from the library.",
                }

    # Cycle suppression check for canonical resolved_key before disk reading
    if tool_context is not None:
        seen_tocs = tool_context.state.get(TEMP_SEEN_TOC_ITEM_KEYS, [])
        if resolved_key in seen_tocs:
            res = {
                "status": "already_inspected",
                "item_key": item_key,
                "message": (
                    f"TOC for {item_key} was already retrieved in this turn. "
                    "Refer to prior context or use read_document for specific sections."
                ),
            }
            if notice:
                _ensure_preliminary_report(tool_context, force=True)
                res["budget_notice"] = notice
            return res

    res = get_document_toc_core(item_key=item_key)

    if isinstance(res, dict) and "item_key" in res:
        resolved_key = res.get("item_key") or resolved_key

    if res.get("status") == "success" and tool_context is not None:
        # Track seen TOC keys for cycle suppression
        seen_tocs = list(tool_context.state.get(TEMP_SEEN_TOC_ITEM_KEYS, []))
        for k in (item_key, resolved_key):
            if k and k not in seen_tocs:
                seen_tocs.append(k)
        abstract_text = ""
        if bundle_dir:
            toc_json_path = bundle_dir / "toc.json"
            if toc_json_path.exists():
                try:
                    import json
                    with open(toc_json_path, encoding="utf-8") as f:
                        toc_data = json.load(f)
                        pk = toc_data.get("parent_item_key")
                        if pk and pk not in seen_tocs:
                            seen_tocs.append(pk)
                        raw_abs = (toc_data.get("abstract") or "").strip()
                        if raw_abs and raw_abs != "No abstract or summary available." and toc_data.get("abstract_source") != "none":
                            abstract_text = raw_abs
                except Exception:
                    pass
        tool_context.state[TEMP_SEEN_TOC_ITEM_KEYS] = seen_tocs

        input_key = item_key
        doc_title = res.get("title") or ""
        parent_key = input_key if input_key != resolved_key else ""

        # Accumulate retrieved abstract into TEMP_RETRIEVED_EVIDENCE so the synthesizer
        # is never starved of evidence when the search agent inspects TOC / abstract.
        if not abstract_text and res.get("toc_content"):
            match = re.search(r"## Abstract\s*\n(.*?)(?=\n## |\Z)", str(res["toc_content"]), re.DOTALL)
            if match:
                raw_abs = match.group(1).strip()
                if raw_abs and raw_abs != "No abstract or summary available.":
                    abstract_text = raw_abs

        if abstract_text:
            doc_authors = None
            doc_year = None
            if bundle_dir:
                try:
                    from zocrates.agents.common.toolkit import get_syncer
                    syncer = get_syncer()
                    if syncer and getattr(syncer, "processor", None):
                        meta = syncer.processor.get_metadata(resolved_key) or {}
                        doc_authors = meta.get("authors") or meta.get("creators")
                        doc_year = meta.get("year")
                except Exception:
                    pass
            if doc_authors is None or doc_year is None:
                active_items = tool_context.state.get(ACTIVE_CONTEXT_ITEMS) or []
                for item in active_items:
                    if isinstance(item, dict):
                        keys = [item.get("key"), item.get("item_key")]
                        for att in item.get("attachments", []):
                            keys.append(att.get("key"))
                        if resolved_key in keys or item_key in keys:
                            if doc_authors is None:
                                doc_authors = item.get("authors") or item.get("creators")
                            if doc_year is None:
                                doc_year = item.get("year")
                            break

            current_evidence = list(tool_context.state.get(TEMP_RETRIEVED_EVIDENCE, []))
            synthetic_chunk_id = f"{resolved_key}:abstract"
            if not any(c.get("chunk_id") == synthetic_chunk_id for c in current_evidence):
                chunk_dict: dict[str, Any] = {
                    "chunk_id": synthetic_chunk_id,
                    "item_key": resolved_key,
                    "title": doc_title or res.get("title", ""),
                    "chunk_type": "text",
                    "heading": "Abstract",
                    "text": abstract_text,
                    "word_count": len(abstract_text.split()),
                    "retrieval_method": "direct_read",
                    "cosine_similarity": None,
                    "rrf_score": None,
                    "rerank_score": None,
                    "image_path": None,
                    "structured_content": None,
                }
                if doc_authors is not None:
                    chunk_dict["authors"] = doc_authors
                if doc_year is not None:
                    chunk_dict["year"] = doc_year
                current_evidence.append(chunk_dict)
                tool_context.state[TEMP_RETRIEVED_EVIDENCE] = current_evidence
        outline_text = ""
        if bundle_dir:
            try:
                outline_text = extract_compact_toc_outline(
                    bundle_dir,
                    doc_title,
                    resolved_key,
                    parent_key=parent_key,
                )
            except Exception as exc:
                logger.debug("Failed to extract compact toc outline: %s", exc)

        if not outline_text and res.get("toc_content"):
            eff_pk = parent_key if parent_key != resolved_key else ""
            key_bracket = f"[Key: {resolved_key}, Parent Key: {eff_pk}]" if eff_pk else f"[Key: {resolved_key}]"
            title_part = f' "{doc_title}"' if doc_title else ""
            lines = [f"### {key_bracket}{title_part}"]
            for raw_line in str(res["toc_content"]).splitlines():
                s = raw_line.strip()
                if s.startswith("- [") or s.startswith("## Figures") or s.startswith("## Tables"):
                    lines.append(s)
            outline_text = "\n".join(lines)

        if outline_text:
            cached_entry = {
                "input_key": input_key,
                "resolved_key": resolved_key,
                "title": doc_title,
                "outline_text": outline_text,
                "updated_at": time.time(),
            }

            # ``or {}`` guards against an explicit None persisted in session state
            # (see matching comment above).
            cached_tocs = dict(tool_context.state.get(CACHED_TOCS) or {})
            cached_tocs[input_key] = cached_entry
            if resolved_key != input_key:
                cached_tocs[resolved_key] = cached_entry

            pruned_tocs = _prune_cached_tocs(cached_tocs, max_entries=MAX_UNIQUE_CACHED_TOCS)
            tool_context.state[CACHED_TOCS] = pruned_tocs

    # Dynamic TOC relation count injection (P1-3)
    if isinstance(res, dict) and res.get("status") == "success" and "toc_content" in res:
        rel_count = 0
        cache_dir = bundle_dir.parent if bundle_dir else None
        if not cache_dir:
            try:
                from zocrates.agents.common.toolkit import get_syncer
                syncer = get_syncer()
                if syncer and getattr(syncer, "processor", None):
                    cache_dir = getattr(syncer.processor, "_cache_dir", None)
            except Exception:
                pass

        if cache_dir:
            meta_db_path = cache_dir / "conversion_meta.db"
            if meta_db_path.exists():
                try:
                    with contextlib.closing(sqlite3.connect(str(meta_db_path), timeout=5.0)) as meta_conn:
                        meta_conn.execute("PRAGMA busy_timeout = 5000;")
                        row = meta_conn.execute(
                            "SELECT COUNT(DISTINCT target_key) FROM item_relations WHERE source_key = ?",
                            (resolved_key,),
                        ).fetchone()
                        if row:
                            rel_count = row[0]
                except Exception as exc:
                    logger.debug("Failed to query item_relations count for %s: %s", resolved_key, exc)

        toc_str = str(res["toc_content"])
        rel_line = f"**Related Items**: {rel_count}"
        if "## Table of Contents" in toc_str:
            idx = toc_str.find("## Table of Contents")
            prefix = toc_str[:idx].rstrip()
            suffix = toc_str[idx:]
            res["toc_content"] = (prefix + "\n\n" if prefix else "") + rel_line + "\n\n" + suffix
        elif "## Sections" in toc_str:
            idx = toc_str.find("## Sections")
            prefix = toc_str[:idx].rstrip()
            suffix = toc_str[idx:]
            res["toc_content"] = (prefix + "\n\n" if prefix else "") + rel_line + "\n\n" + suffix
        else:
            res["toc_content"] = toc_str.rstrip() + f"\n\n{rel_line}\n"

    if notice and tool_context is not None:
        _ensure_preliminary_report(tool_context, force=True)
        res["budget_notice"] = notice
    return res


def read_document(
    item_key: str,
    asset_id: str | list[str] | None = None,
    page: int | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Read full document Markdown or targeted sections, figures, or tables from the OKF bundle.

    Accepts exact asset IDs from an item's TOC (e.g., 's1', 's2', 'f1', 't1'), 'abstract' (or 'summary') to read the document's abstract, a list of IDs, or fuzzy heading names.

    Args:
        item_key: Unique Zotero item key.
        asset_id: Asset ID from TOC, list of asset IDs, or fuzzy heading string.
            If None (default), reads whole document paginated.
        page: 1-indexed page number if content exceeds ~3,000 words.

    Returns:
        Dictionary with 'status', 'item_key', and list of 'assets'.
    """
    allowed, err_msg, notice = _check_and_increment_tool_budget(tool_context)
    if not allowed:
        return {"status": "budget_exhausted", "error": err_msg}

    norm_asset_ids = normalize_asset_ids(asset_id)

    # Intra-tool deduplication check using canonical keys only
    if tool_context is not None:
        seen_assets = set(tool_context.state.get(TEMP_SEEN_ASSETS, []))
        if norm_asset_ids is None:
            full_key = f"{item_key}:full"
            if full_key in seen_assets:
                res = {
                    "status": "success",
                    "item_key": item_key,
                    "assets": [],
                    "notice": f"[Full document {item_key} was already retrieved in this session.]",
                }
                if notice:
                    _ensure_preliminary_report(tool_context, force=True)
                    res["budget_notice"] = notice
                return res
        else:
            unseen = [aid for aid in norm_asset_ids if f"{item_key}:{aid}" not in seen_assets]
            if not unseen:
                res = {
                    "status": "success",
                    "item_key": item_key,
                    "assets": [],
                    "notice": f"[All requested assets for {item_key} ({', '.join(norm_asset_ids)}) were already retrieved in this session.]",
                }
                if notice:
                    _ensure_preliminary_report(tool_context, force=True)
                    res["budget_notice"] = notice
                return res
            norm_asset_ids = unseen

    call_aid: str | list[str] | None = (
        norm_asset_ids[0]
        if (norm_asset_ids is not None and len(norm_asset_ids) == 1)
        else norm_asset_ids
    )
    res = read_document_core(item_key=item_key, asset_id=call_aid, page=page)

    if res.get("status") == "success" and tool_context is not None:
        assets = res.get("assets", [])
        intersecting_ids = res.get("intersecting_chunk_ids", [])

        # Update temp:seen_assets with CANONICAL keys (and single requested alias if resolved)
        seen_assets = set(tool_context.state.get(TEMP_SEEN_ASSETS, []))
        if norm_asset_ids is None:
            seen_assets.add(f"{item_key}:full")
        elif len(norm_asset_ids) == 1 and assets:
            seen_assets.add(f"{item_key}:{norm_asset_ids[0]}")

        for asset in assets:
            resolved_aid = asset.get("asset_id", "full")
            seen_assets.add(f"{item_key}:{resolved_aid}")
        tool_context.state[TEMP_SEEN_ASSETS] = list(seen_assets)

        # Range-Aware Pre-Exclusion: Add intersecting chunk IDs to temp:seen_chunk_ids
        seen_chunk_ids = set(tool_context.state.get(TEMP_SEEN_CHUNK_IDS, []))
        seen_chunk_ids.update(intersecting_ids)
        tool_context.state[TEMP_SEEN_CHUNK_IDS] = list(seen_chunk_ids)

        # Convert read assets to synthetic evidence chunks in temp:retrieved_evidence (deduping by chunk_id)
        doc_title = res.get("title") or ""
        doc_authors = res.get("authors")
        doc_year = res.get("year")
        if tool_context is not None and (not doc_title or doc_authors is None or doc_year is None):
            active_items = tool_context.state.get(ACTIVE_CONTEXT_ITEMS, [])
            if isinstance(active_items, list):
                for item in active_items:
                    if isinstance(item, dict) and (item.get("item_key") or item.get("key")) == item_key:
                        if not doc_title:
                            doc_title = item.get("title", "")
                        if doc_authors is None:
                            doc_authors = item.get("authors") or item.get("creators")
                        if doc_year is None:
                            doc_year = item.get("year")
                        break

        # Canonical bundle key: read_document_core resolves scope aliases (e.g.
        # a parent item key whose PDF bundle lives under an attachment key) to
        # the key the bundle is actually stored under.  Evidence chunks must
        # carry the RESOLVED key — the /assets endpoint serves files from
        # cache_dir/<item_key>, so a requested-but-unresolved key would 404
        # the figure image in the sidebar.
        resolved_bundle_key = str(res.get("item_key") or item_key).strip() or item_key

        current_evidence = list(tool_context.state.get(TEMP_RETRIEVED_EVIDENCE, []))
        existing_evidence_ids = {
            e.get("chunk_id") for e in current_evidence if e.get("chunk_id")
        }
        for asset in assets:
            synthetic_chunk_id = f"{resolved_bundle_key}:{asset.get('asset_id', 'full')}"
            if synthetic_chunk_id in existing_evidence_ids:
                continue
            existing_evidence_ids.add(synthetic_chunk_id)
            chunk_dict: dict[str, Any] = {
                "chunk_id": synthetic_chunk_id,
                "item_key": resolved_bundle_key,
                "title": doc_title,
                "chunk_type": asset.get("asset_type", "text"),
                "heading": asset.get("heading", ""),
                "text": asset.get("content", ""),
                "word_count": asset.get("word_count", 0),
                "retrieval_method": "direct_read",
                "cosine_similarity": None,
                "rrf_score": None,
                "rerank_score": None,
                "image_path": asset.get("path") if asset.get("asset_type") == "figure" else None,
                "structured_content": asset.get("structured_content"),
            }
            if doc_authors is not None:
                chunk_dict["authors"] = doc_authors
            if doc_year is not None:
                chunk_dict["year"] = doc_year
            current_evidence.append(chunk_dict)
        tool_context.state[TEMP_RETRIEVED_EVIDENCE] = current_evidence

        total_words = sum(c.get("word_count", 0) for c in current_evidence)
        res["retrieval_status"] = (
            f"[Retrieval status: ~{total_words} words accumulated in evidence set; "
            f"excluded {len(intersecting_ids)} overlapping vector chunks from future searches]"
        )

    if notice and tool_context is not None:
        _ensure_preliminary_report(tool_context, force=True)
        res["budget_notice"] = notice

    return res


def request_search_extension(
    justification: str,
    covered_summary: str,
    missing_aspects: list[str],
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Request permission from the user to extend the retrieval budget beyond the current limit.

    Use this tool when evidence volume or retrieval tool budget is approaching its ceiling
    and crucial literature aspects or candidate papers remain unaddressed.

    Args:
        justification: Clear reason why additional retrieval calls or evidence capacity are needed.
        covered_summary: Brief summary of the findings and evidence collected so far.
        missing_aspects: List of specific unaddressed aspects, papers, or topics still needed.

    Returns:
        Dictionary confirming extension request registration and turn conclusion.
    """
    if tool_context is not None:
        tool_context.state[TEMP_SEARCH_REPORT] = {
            "summary": covered_summary,
            "gaps": missing_aspects if isinstance(missing_aspects, list) else [str(missing_aspects)],
            "notes": justification,
            "source": "subagent_request",
        }
        tool_context.state[TEMP_EXTENSION_REQUESTED] = True
        ext_num = int(tool_context.state.get(TEMP_EXTENSION_COUNT, 0)) + 1
        tool_context.state[TEMP_EXTENSION_REQUEST_PASS] = ext_num
        tool_context.actions.skip_summarization = True

    return {
        "status": "extension_requested",
        "message": "Extension request registered. Concluding retrieval turn for user approval.",
    }


def get_related_items(
    item_key: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve all verified related papers for a given document from the knowledge graph.

    Args:
        item_key: Unique Zotero item or attachment key.

    Returns:
        Dictionary with status, item_key, total_count, and list of related items.
    """
    allowed, err_msg, notice = _check_and_increment_tool_budget(tool_context)
    if not allowed:
        return {"status": "budget_exhausted", "error": err_msg}

    res = get_related_items_core(item_key=item_key, ceiling=25)

    if notice and tool_context is not None:
        _ensure_preliminary_report(tool_context, force=True)
        res["budget_notice"] = notice

    return res


def get_common_tools() -> list[Any]:
    """Return the list of core ADK tools for research agents."""
    return [
        search_corpus,
        get_document_toc,
        read_document,
        get_related_items,
        conclude_search,
        request_search_extension,
    ]
