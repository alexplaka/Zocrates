#!/usr/bin/env python3
"""Manual retrieval testing diagnostic script for ChromaDB and SQLite FTS5.

Supports semantic search (dense vector / cosine similarity), keyword search (BM25 FTS5),
hybrid search (Reciprocal Rank Fusion), and side-by-side comparison mode with Jaccard overlap.

Usage:
    # Diagnostic stats
    uv run python scripts/test_retrieval.py --stats

    # Compare semantic vs keyword search side-by-side
    uv run python scripts/test_retrieval.py "transformer attention" --mode compare -k 3

    # Semantic search only
    uv run python scripts/test_retrieval.py "attention is all you need" --mode semantic -k 5

    # Keyword search only
    uv run python scripts/test_retrieval.py "multi-head self-attention" --mode keyword -k 5

    # Hybrid search only
    uv run python scripts/test_retrieval.py "residual connections" --mode hybrid -k 5

    # Interactive REPL mode (default if no query is passed)
    uv run python scripts/test_retrieval.py
"""

import argparse
import logging
import re
try:
    import readline  # noqa: F401 - enables terminal arrow-key line editing on Unix
except ImportError:
    pass
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb

from zocrates.config import (
    get_chroma_dir,
    get_fts_db_path,
    get_secure_api_key,
    get_semantic_similarity_threshold,
    load_zocrates_config,
    resolve_zotero_data_dir,
)
from zocrates.rag.embeddings import OpenRouterEmbedder, get_embedder
from zocrates.rag.vector_store import SearchResult, VectorStore
from zocrates.rag.zotero_db import ZoteroDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal Styling
# ---------------------------------------------------------------------------

class Style:
    """ANSI color codes for formatted terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    RED = "\033[31m"


def paint(text: str, color: str) -> str:
    """Format text with ANSI escape sequence if stdout is an interactive tty."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{Style.RESET}"


# ---------------------------------------------------------------------------
# Metadata Enrichment
# ---------------------------------------------------------------------------

class MetadataResolver:
    """Enriches retrieved search results with Zotero bibliographic metadata."""

    def __init__(self, zotero_dir: Path | None) -> None:
        self._zotero_db: ZoteroDB | None = None
        self._cache: dict[str, tuple[str, list[str], str | None]] = {}
        if zotero_dir is not None:
            sqlite_path = zotero_dir / "zotero.sqlite"
            storage_dir = zotero_dir / "storage"
            if sqlite_path.exists() and storage_dir.exists():
                self._zotero_db = ZoteroDB(sqlite_path, storage_dir)

    def _fetch_item_meta(self, item_key: str, item_id: int) -> tuple[str, list[str], str | None]:
        """Fetch title, creators, and year for an item from Zotero DB."""
        if self._zotero_db is None:
            return ("", [], None)
        item = (
            self._zotero_db.get_item(item_id)
            if item_id
            else self._zotero_db.get_item_by_key(item_key)
        )
        if item is None:
            return ("", [], None)
        creators = [c for c in item.creators if isinstance(c, str)]
        return (item.title or "", creators, item.year)

    def enrich(self, results: list[SearchResult]) -> None:
        """Populate title, creators, and publication year on each search result."""
        for r in results:
            if not r.item_key:
                continue
            if r.item_key not in self._cache:
                self._cache[r.item_key] = self._fetch_item_meta(r.item_key, r.item_id)
            title, creators, year = self._cache[r.item_key]
            r.title = title
            r.creators = creators
            r.year = year


# ---------------------------------------------------------------------------
# Retrieval Runners & Filter Normalization
# ---------------------------------------------------------------------------

def normalize_chunk_type(chunk_type: str | list[str] | None) -> str | list[str] | None:
    """Normalize chunk type filter. Values like 'all', '*', 'any', 'none', 'null', '' map to None."""
    if chunk_type is None:
        return None
    if isinstance(chunk_type, str):
        ct = chunk_type.strip().lower()
        if ct in ("all", "*", "any", "none", "null", ""):
            return None
        return ct
    if isinstance(chunk_type, (list, set, tuple)):
        filtered = [
            t.strip().lower()
            for t in chunk_type
            if isinstance(t, str) and t.strip().lower() not in ("all", "*", "any", "none", "null", "")
        ]
        return filtered if filtered else None
    return None


def run_semantic_search(
    store: VectorStore,
    query: str,
    top_k: int = 5,
    item_key_filter: list[str] | None = None,
    chunk_type_filter: str | list[str] | None = None,
    similarity_threshold: float | None = None,
) -> list[SearchResult]:
    """Execute dense vector similarity retrieval directly against ChromaDB."""
    scored = store._semantic_search_with_scores(
        query=query,
        top_k=top_k,
        item_key_filter=item_key_filter,
        chunk_type_filter=normalize_chunk_type(chunk_type_filter),
        similarity_threshold=similarity_threshold,
    )
    if not scored:
        return []

    chunk_ids = [cid for cid, _ in scored]
    chunk_data = store._fetch_chunks(chunk_ids)

    results: list[SearchResult] = []
    for cid, score in scored:
        if cid not in chunk_data:
            continue
        data = chunk_data[cid]
        heading_raw = data.get("heading", "")
        heading_path = heading_raw.split(" > ") if heading_raw else []
        results.append(
            SearchResult(
                chunk_text=data.get("text", ""),
                item_key=data.get("item_key", ""),
                item_id=data.get("item_id", 0),
                heading_path=heading_path,
                chunk_index=data.get("chunk_index", 0),
                score=score,
                chunk_type=data.get("chunk_type", "text"),
                image_path=data.get("image_path"),
                caption_source=data.get("caption_source"),
                vlm_model=data.get("vlm_model"),
                raw_semantic_score=score,
                raw_keyword_rank=None,
                retrieval_method="semantic",
                chunk_id=cid,
                line_start=data.get("line_start"),
                line_end=data.get("line_end"),
            )
        )
    return results


def run_keyword_search(
    store: VectorStore,
    query: str,
    top_k: int = 5,
    item_key_filter: list[str] | None = None,
    chunk_type_filter: str | list[str] | None = None,
) -> list[SearchResult]:
    """Execute BM25 keyword search directly against SQLite FTS5 index."""
    ranked = store._keyword_search_with_ranks(
        query=query,
        top_k=top_k,
        item_key_filter=item_key_filter,
        chunk_type_filter=normalize_chunk_type(chunk_type_filter),
    )
    if not ranked:
        return []

    chunk_ids = [cid for cid, _ in ranked]
    chunk_data = store._fetch_chunks(chunk_ids)

    results: list[SearchResult] = []
    for cid, rank in ranked:
        if cid not in chunk_data:
            continue
        data = chunk_data[cid]
        heading_raw = data.get("heading", "")
        heading_path = heading_raw.split(" > ") if heading_raw else []
        results.append(
            SearchResult(
                chunk_text=data.get("text", ""),
                item_key=data.get("item_key", ""),
                item_id=data.get("item_id", 0),
                heading_path=heading_path,
                chunk_index=data.get("chunk_index", 0),
                score=float(rank),
                chunk_type=data.get("chunk_type", "text"),
                image_path=data.get("image_path"),
                caption_source=data.get("caption_source"),
                vlm_model=data.get("vlm_model"),
                raw_semantic_score=None,
                raw_keyword_rank=rank,
                retrieval_method="keyword",
                chunk_id=cid,
                line_start=data.get("line_start"),
                line_end=data.get("line_end"),
            )
        )
    return results


def run_hybrid_search(
    store: VectorStore,
    query: str,
    top_k: int = 20,
    final_k: int = 5,
    item_key_filter: list[str] | None = None,
    chunk_type_filter: str | list[str] | None = None,
) -> list[SearchResult]:
    """Execute hybrid search using Reciprocal Rank Fusion via VectorStore."""
    return store.search(
        query=query,
        top_k=top_k,
        final_k=final_k,
        item_key_filter=item_key_filter,
        chunk_type_filter=normalize_chunk_type(chunk_type_filter),
    )


# ---------------------------------------------------------------------------
# Statistics Display
# ---------------------------------------------------------------------------

def display_stats(zotero_dir: Path | None) -> None:
    """Print database statistics for ChromaDB collections and SQLite FTS5 index."""
    print()
    print(paint("=== Zocrates Retrieval Index Statistics ===", Style.BOLD + Style.CYAN))
    if zotero_dir is None:
        print(paint("Error: Could not locate Zotero data directory.", Style.RED))
        return

    print(f"{paint('Zotero Data Dir:', Style.BOLD)} {zotero_dir}")
    cfg = load_zocrates_config(zotero_dir)
    active_prov = cfg.get("embedding_provider", "local")
    active_model = cfg.get("embedding_model", "jinaai/jina-embeddings-v5-text-nano")
    threshold = cfg.get("semantic_similarity_threshold", 0.50)

    print(f"{paint('Active Config:', Style.BOLD)} provider={active_prov}, model={active_model}, similarity_threshold={threshold}")

    # ChromaDB inspection
    chroma_dir = get_chroma_dir(zotero_dir)
    print()
    print(paint(f"[ChromaDB Persistent Store] {chroma_dir}", Style.BOLD + Style.YELLOW))
    if chroma_dir.exists():
        client = chromadb.PersistentClient(path=str(chroma_dir))
        collections = client.list_collections()
        if not collections:
            print("  (No collections found)")
        for col_entry in collections:
            col_name = col_entry if isinstance(col_entry, str) else col_entry.name
            col = client.get_collection(col_name)
            meta = col.metadata or {}
            dim = meta.get("dimension", "unknown")
            model = meta.get("model_name", "unknown")
            space = meta.get("hnsw:space", "cosine")
            print(f"  • {paint(col_name, Style.BOLD)}: {paint(str(col.count()), Style.GREEN)} chunks (model: {model}, dim: {dim}, space: {space})")
    else:
        print("  (Chroma directory does not exist)")

    # SQLite FTS5 inspection
    fts_path = get_fts_db_path(zotero_dir)
    print()
    print(paint(f"[SQLite FTS5 Index] {fts_path}", Style.BOLD + Style.YELLOW))
    if fts_path.exists():
        conn = sqlite3.connect(fts_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM chunks_fts")
        fts_chunks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT item_key) FROM chunk_meta")
        unique_items = cur.fetchone()[0]
        cur.execute("SELECT chunk_type, COUNT(*) FROM chunk_meta GROUP BY chunk_type ORDER BY COUNT(*) DESC")
        type_counts = cur.fetchall()
        conn.close()

        print(f"  • Total FTS Chunks: {paint(str(fts_chunks), Style.GREEN)}")
        print(f"  • Unique Documents Indexed: {paint(str(unique_items), Style.GREEN)}")
        if type_counts:
            breakdown = ", ".join(f"{t}: {cnt}" for t, cnt in type_counts)
            print(f"  • Chunk Type Breakdown: {breakdown}")
    else:
        print("  (FTS index database does not exist)")
    print()


# ---------------------------------------------------------------------------
# Formatting & Presentation
# ---------------------------------------------------------------------------

def format_score_badge(result: SearchResult, mode: str) -> str:
    """Format score and rank badge according to retrieval mode."""
    if mode == "semantic":
        score = result.raw_semantic_score if result.raw_semantic_score is not None else result.score
        return f"Sim: {paint(f'{score:.4f}', Style.GREEN + Style.BOLD)}"
    if mode == "keyword":
        rank = result.raw_keyword_rank if result.raw_keyword_rank is not None else int(result.score)
        return f"BM25 Rank: {paint(f'#{rank}', Style.YELLOW + Style.BOLD)}"

    # Hybrid mode
    parts = [f"RRF: {paint(f'{result.score:.4f}', Style.GREEN + Style.BOLD)}"]
    if result.raw_semantic_score is not None:
        parts.append(f"sem={result.raw_semantic_score:.4f}")
    if result.raw_keyword_rank is not None:
        parts.append(f"kw=#{result.raw_keyword_rank}")
    parts.append(f"via={result.retrieval_method}")
    return " | ".join(parts)


def print_chunk_card(
    idx: int,
    result: SearchResult,
    mode: str,
    *,
    full_text: bool = False,
) -> None:
    """Print an individual retrieved chunk card with provenance metadata."""
    score_badge = format_score_badge(result, mode)
    header = f"[{idx}] {result.chunk_id}  ({score_badge})"
    print(paint(header, Style.BOLD + Style.CYAN))

    # Provenance line: Title / Authors / Year
    provenance_parts: list[str] = []
    if result.title:
        provenance_parts.append(paint(result.title, Style.BOLD))
    if result.creators:
        author_str = ", ".join(result.creators[:2])
        if len(result.creators) > 2:
            author_str += " et al."
        provenance_parts.append(author_str)
    if result.year:
        provenance_parts.append(f"({result.year})")

    if provenance_parts:
        print("    " + " • ".join(provenance_parts))

    # Location line: Item key, chunk type, heading, line numbers
    meta_parts = [
        f"item: {paint(result.item_key, Style.YELLOW)}",
        f"type: {result.chunk_type}",
    ]
    if result.heading_path:
        meta_parts.append(f"section: {' > '.join(result.heading_path)}")
    if result.line_start is not None and result.line_end is not None:
        meta_parts.append(f"lines {result.line_start}-{result.line_end}")

    print("    " + " | ".join(meta_parts))

    # Text snippet
    raw_text = (result.chunk_text or "").strip()
    if not full_text and len(raw_text) > 300:
        snippet = raw_text[:300].replace("\n", " ").strip() + "..."
    elif not full_text:
        snippet = raw_text.replace("\n", " ").strip()
    else:
        snippet = raw_text

    print(f"    {paint('Text:', Style.DIM)} {snippet}")
    print()


def display_results_section(
    title: str,
    results: list[SearchResult],
    mode: str,
    *,
    full_text: bool = False,
    threshold: float | None = None,
    query: str = "",
) -> None:
    """Print a labeled section of retrieval results."""
    banner = f"--- {title} ({len(results)} matches) ---"
    print(paint(banner, Style.BOLD + Style.BLUE))

    if mode == "keyword" and query:
        clean_query = re.sub(r'[\*\":\(\)\-\^]', " ", query)
        clean_query = re.sub(r"\bNEAR(/\d+)?\b", " ", clean_query, flags=re.IGNORECASE)
        tokens = [t for t in clean_query.split() if t]
        if any(len(t) < 3 for t in tokens):
            print(
                paint(
                    "  Note: SQLite FTS5 trigram index requires tokens >= 3 characters. "
                    "Words shorter than 3 characters are ignored by BM25.",
                    Style.YELLOW,
                )
            )

    if not results:
        print("  No matching chunks found.")
        if mode == "semantic" and threshold is not None and threshold > 0.0:
            print(
                paint(
                    f"  Tip: No chunks met similarity threshold ({threshold}). "
                    "Try lowering it with :threshold 0.3 or :threshold 0.0 to inspect raw similarity scores.",
                    Style.YELLOW,
                )
            )
        print()
        return

    for idx, res in enumerate(results, start=1):
        print_chunk_card(idx, res, mode, full_text=full_text)


def display_comparison_summary(
    semantic_results: list[SearchResult],
    keyword_results: list[SearchResult],
) -> None:
    """Print Jaccard overlap, shared chunks, and unique matches between retrieval methods."""
    sem_ids = [r.chunk_id for r in semantic_results]
    kw_ids = [r.chunk_id for r in keyword_results]

    set_sem = set(sem_ids)
    set_kw = set(kw_ids)

    shared = set_sem & set_kw
    sem_only = set_sem - set_kw
    kw_only = set_kw - set_sem
    union = set_sem | set_kw

    jaccard = len(shared) / len(union) if union else 0.0

    print(paint("=== Comparison Summary ===", Style.BOLD + Style.MAGENTA))
    print(f"  • Overlap (Jaccard Index): {paint(f'{jaccard:.2%}', Style.BOLD + Style.GREEN)} ({len(shared)} / {len(union)} distinct chunks)")
    print(f"  • Shared in both top-k:    {paint(str(len(shared)), Style.GREEN)}")
    print(f"  • Unique to Semantic:      {paint(str(len(sem_only)), Style.YELLOW)}")
    print(f"  • Unique to Keyword:       {paint(str(len(kw_only)), Style.YELLOW)}")

    if shared:
        print()
        print(paint("  Shared Chunk Rank Cross-Reference:", Style.BOLD))
        for cid in sorted(shared):
            s_rank = sem_ids.index(cid) + 1
            k_rank = kw_ids.index(cid) + 1
            delta = k_rank - s_rank
            delta_str = f"(diff: {delta:+d})" if delta != 0 else "(exact match)"
            print(f"    • {paint(cid, Style.CYAN)}: Semantic rank #{s_rank} | Keyword rank #{k_rank} {delta_str}")
    print()


# ---------------------------------------------------------------------------
# Interactive REPL Loop
# ---------------------------------------------------------------------------

@dataclass
class ReplState:
    """State for interactive REPL session."""

    mode: str
    limit: int
    threshold: float | None
    chunk_type: str | None
    item_keys: list[str] | None
    full_text: bool


def _handle_mode_command(new_mode: str, state: ReplState) -> None:
    cleaned = new_mode.lower()
    if cleaned in ("compare", "semantic", "keyword", "hybrid"):
        state.mode = cleaned
        print(f"Mode set to: {paint(state.mode, Style.GREEN)}")
    else:
        print("Valid modes: compare, semantic, keyword, hybrid")


def _handle_limit_command(limit_str: str, state: ReplState) -> None:
    if limit_str.isdigit():
        state.limit = max(1, int(limit_str))
        print(f"Limit set to: {paint(str(state.limit), Style.GREEN)}")
    else:
        print("Limit must be a positive integer.")


def _handle_threshold_command(thresh_str: str, state: ReplState) -> None:
    stripped = thresh_str.strip()
    valid_float = stripped.replace(".", "", 1).isdigit() or (
        stripped.startswith("-") and stripped[1:].replace(".", "", 1).isdigit()
    )
    if valid_float:
        state.threshold = float(stripped)
        print(f"Similarity threshold set to: {state.threshold}")
    else:
        print("Threshold must be a valid float.")


def _print_repl_help() -> None:
    print("Available Commands:")
    print("  :mode <compare|semantic|keyword|hybrid>  Set search mode")
    print("  :limit <number>                          Set max results (top-k)")
    print("  :filter <key1,key2>                      Filter by item key")
    print("  :type <text|figure|table|code|equation>  Filter by chunk type")
    print("  :threshold <float>                       Set cosine similarity threshold")
    print("  :full                                    Toggle full chunk text display")
    print("  :clear                                   Clear active filters")
    print("  :stats                                   Display database statistics")
    print("  :quit, :exit                             Exit the REPL loop")


def handle_repl_command(
    cmd_str: str,
    state: ReplState,
    init_threshold: float | None,
) -> bool:
    """Handle REPL command. Returns True to continue loop, False to exit."""
    cmd_parts = shlex.split(cmd_str[1:])
    if not cmd_parts:
        return True
    verb = cmd_parts[0].lower()
    args = cmd_parts[1:]

    if verb in ("q", "quit", "exit"):
        print("Goodbye.")
        return False
    if verb in ("m", "mode") and args:
        _handle_mode_command(args[0], state)
    elif verb in ("k", "limit") and args:
        _handle_limit_command(args[0], state)
    elif verb in ("f", "full"):
        state.full_text = not state.full_text
        status = "ON" if state.full_text else "OFF"
        print(f"Full text preview: {paint(status, Style.GREEN)}")
    elif verb in ("i", "filter", "item") and args:
        state.item_keys = [a.strip() for a in args[0].split(",") if a.strip()]
        print(f"Item key filter set to: {state.item_keys}")
    elif verb in ("t", "type") and args:
        raw_type = args[0].strip().lower()
        if raw_type in ("all", "*", "any", "none", "null", ""):
            state.chunk_type = None
            print("Chunk type filter cleared (searching all chunk types).")
        else:
            state.chunk_type = raw_type
            print(f"Chunk type filter set to: {state.chunk_type}")
    elif verb in ("th", "threshold") and args:
        _handle_threshold_command(args[0], state)
    elif verb in ("c", "clear"):
        state.item_keys = None
        state.chunk_type = None
        state.threshold = init_threshold
        state.full_text = False
        print("Filters reset to defaults.")
    elif verb in ("s", "stats"):
        display_stats(resolve_zotero_data_dir())
    elif verb in ("h", "help"):
        _print_repl_help()
    else:
        print(f"Unknown command: :{verb}. Type :help for commands.")
    return True


def run_interactive_repl(
    store: VectorStore,
    resolver: MetadataResolver,
    init_mode: str = "compare",
    init_limit: int = 5,
    init_threshold: float | None = None,
    init_chunk_type: str | None = None,
    init_item_keys: list[str] | None = None,
    *,
    init_full: bool = False,
) -> None:
    """Run an interactive REPL loop to rapidly evaluate queries."""
    state = ReplState(
        mode=init_mode,
        limit=init_limit,
        threshold=init_threshold,
        chunk_type=init_chunk_type,
        item_keys=init_item_keys,
        full_text=init_full,
    )

    print()
    print(paint("==========================================================", Style.BOLD + Style.CYAN))
    print(paint("          Zocrates Retrieval Testing REPL Interactive     ", Style.BOLD + Style.CYAN))
    print(paint("==========================================================", Style.BOLD + Style.CYAN))
    print(f"Active Collection: {paint(store.active_collection_name, Style.GREEN)}")
    print(f"Defaults: mode={paint(state.mode, Style.YELLOW)}, limit={state.limit}, threshold={state.threshold or 'default'}")
    print("Commands: :mode <name>, :limit <k>, :filter <item_key>, :type <chunk_type>,")
    print("          :threshold <val>, :full, :stats, :clear, :help, :quit")
    print()

    while True:
        try:
            prompt = f"search ({state.mode}, k={state.limit})> "
            query = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting REPL.")
            break

        if not query:
            continue

        if query.startswith(":"):
            should_continue = handle_repl_command(query, state, init_threshold)
            if not should_continue:
                break
            continue

        t0 = time.perf_counter()
        try:
            execute_query(
                store=store,
                resolver=resolver,
                query=query,
                mode=state.mode,
                limit=state.limit,
                threshold=state.threshold,
                chunk_type=state.chunk_type,
                item_keys=state.item_keys,
                full_text=state.full_text,
            )
        except Exception as exc:
            print(paint(f"Error during query execution: {exc}", Style.RED))
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(paint(f"[Query completed in {elapsed_ms:.1f} ms]", Style.DIM))
        print()


# ---------------------------------------------------------------------------
# Core Query Dispatcher
# ---------------------------------------------------------------------------

def execute_query(
    store: VectorStore,
    resolver: MetadataResolver,
    query: str,
    mode: str,
    limit: int,
    threshold: float | None,
    chunk_type: str | None,
    item_keys: list[str] | None,
    *,
    full_text: bool,
) -> None:
    """Run the requested search mode and render results."""
    chunk_type = normalize_chunk_type(chunk_type)
    print()
    print(paint(f"Query: \"{query}\"", Style.BOLD))
    filters = []
    if item_keys:
        filters.append(f"items={item_keys}")
    if chunk_type:
        filters.append(f"type={chunk_type}")
    if mode in ("semantic", "compare") and threshold is not None:
        filters.append(f"threshold={threshold}")
    if filters:
        print(paint(f"Filters: {', '.join(filters)}", Style.DIM))
    print()

    if mode == "semantic":
        results = run_semantic_search(
            store=store,
            query=query,
            top_k=limit,
            item_key_filter=item_keys,
            chunk_type_filter=chunk_type,
            similarity_threshold=threshold,
        )
        resolver.enrich(results)
        display_results_section(
            "Semantic Results (Cosine Similarity)",
            results,
            "semantic",
            full_text=full_text,
            threshold=threshold,
            query=query,
        )

    elif mode == "keyword":
        results = run_keyword_search(
            store=store,
            query=query,
            top_k=limit,
            item_key_filter=item_keys,
            chunk_type_filter=chunk_type,
        )
        resolver.enrich(results)
        display_results_section(
            "Keyword Results (BM25 FTS5)",
            results,
            "keyword",
            full_text=full_text,
            threshold=threshold,
            query=query,
        )

    elif mode == "hybrid":
        results = run_hybrid_search(
            store=store,
            query=query,
            top_k=max(20, limit * 3),
            final_k=limit,
            item_key_filter=item_keys,
            chunk_type_filter=chunk_type,
        )
        resolver.enrich(results)
        display_results_section(
            "Hybrid Results (Reciprocal Rank Fusion)",
            results,
            "hybrid",
            full_text=full_text,
            threshold=threshold,
            query=query,
        )

    else:  # compare mode
        sem_res = run_semantic_search(
            store=store,
            query=query,
            top_k=limit,
            item_key_filter=item_keys,
            chunk_type_filter=chunk_type,
            similarity_threshold=threshold,
        )
        kw_res = run_keyword_search(
            store=store,
            query=query,
            top_k=limit,
            item_key_filter=item_keys,
            chunk_type_filter=chunk_type,
        )
        resolver.enrich(sem_res)
        resolver.enrich(kw_res)

        display_results_section(
            "Semantic Search (ChromaDB Cosine)",
            sem_res,
            "semantic",
            full_text=full_text,
            threshold=threshold,
            query=query,
        )
        display_results_section(
            "Keyword Search (SQLite BM25)",
            kw_res,
            "keyword",
            full_text=full_text,
            threshold=threshold,
            query=query,
        )
        display_comparison_summary(sem_res, kw_res)


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Manual retrieval diagnostic and test runner for ChromaDB and SQLite FTS5.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Search query text. If omitted and --stats is not specified, launches interactive REPL.",
    )
    parser.add_argument(
        "-m", "--mode",
        choices=["compare", "semantic", "keyword", "hybrid"],
        default="compare",
        help="Search mode to execute.",
    )
    parser.add_argument(
        "-k", "--limit",
        type=int,
        default=5,
        help="Maximum number of results to retrieve.",
    )
    parser.add_argument(
        "-i", "--item-key",
        dest="item_keys",
        action="append",
        help="Zotero item key to filter by (can specify multiple times or comma-separated).",
    )
    parser.add_argument(
        "-t", "--chunk-type",
        choices=["text", "frontmatter", "figure", "table", "equation", "code", "all"],
        default=None,
        help="Filter results to a specific chunk type ('all' to search all types).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Minimum cosine similarity threshold for semantic retrieval (overrides config default).",
    )
    parser.add_argument(
        "-e", "--embed-mode",
        choices=["local", "cloud"],
        default=None,
        help="Embedding mode override (default reads from active zocrates_config.json).",
    )
    parser.add_argument(
        "-f", "--full",
        action="store_true",
        help="Display full chunk text instead of a truncated preview snippet.",
    )
    parser.add_argument(
        "-s", "--stats",
        action="store_true",
        help="Display database collection and index statistics, then exit.",
    )
    parser.add_argument(
        "-I", "--interactive",
        action="store_true",
        help="Launch interactive REPL query session.",
    )
    return parser


def parse_item_keys(keys_arg: list[str] | None) -> list[str] | None:
    """Normalize item key arguments into a clean list of keys."""
    if not keys_arg:
        return None
    normalized: list[str] = []
    for entry in keys_arg:
        for key in entry.split(","):
            cleaned = key.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
    return normalized if normalized else None


def main() -> None:
    """Main script entry point."""
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s [%(name)s]: %(message)s",
    )

    parser = build_parser()
    args = parser.parse_args()

    # Discover active Zotero profile
    zotero_dir = resolve_zotero_data_dir()
    if zotero_dir is None:
        print(paint("Error: Could not locate Zotero data directory.", Style.RED))
        print("Please ensure Zotero has been run or set ZOTERO_DATA_DIR.")
        sys.exit(1)

    # Handle --stats flag
    if args.stats:
        display_stats(zotero_dir)
        sys.exit(0)

    # Initialize embedder and VectorStore
    embedder = get_embedder(mode=args.embed_mode)
    chroma_dir = get_chroma_dir(zotero_dir)
    fts_path = get_fts_db_path(zotero_dir)

    store = VectorStore(
        chroma_dir=chroma_dir,
        fts_db_path=fts_path,
        embedder=embedder,
    )

    # Startup preflight checks
    if isinstance(embedder, OpenRouterEmbedder):
        api_key = get_secure_api_key(
            "openrouter_api_key",
            ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"),
        )
        if not api_key:
            print(
                paint(
                    "WARNING: OPENROUTER_API_KEY is not configured in environment or keyring.\n"
                    "         Semantic retrieval will return 0 results unless OPENROUTER_API_KEY\n"
                    "         is exported or --embed-mode local is used.",
                    Style.YELLOW + Style.BOLD,
                )
            )
            print()

    try:
        if store._collection.count() == 0:
            print(
                paint(
                    f"WARNING: Active Chroma collection '{store.active_collection_name}' contains 0 chunks.\n"
                    "         Semantic retrieval will yield no results until documents are indexed.",
                    Style.YELLOW + Style.BOLD,
                )
            )
            print()
    except Exception as exc:
        logger.warning("Could not verify Chroma collection chunk count: %s", exc)

    resolver = MetadataResolver(zotero_dir)

    limit = max(1, args.limit)
    item_keys = parse_item_keys(args.item_keys)
    threshold = args.threshold if args.threshold is not None else get_semantic_similarity_threshold()
    chunk_type = normalize_chunk_type(args.chunk_type)

    # Launch REPL if requested or if no positional query provided
    if args.interactive or not args.query:
        run_interactive_repl(
            store=store,
            resolver=resolver,
            init_mode=args.mode,
            init_limit=limit,
            init_threshold=threshold,
            init_chunk_type=chunk_type,
            init_item_keys=item_keys,
            init_full=args.full,
        )
        return

    # Single-shot query execution
    execute_query(
        store=store,
        resolver=resolver,
        query=args.query,
        mode=args.mode,
        limit=limit,
        threshold=threshold,
        chunk_type=chunk_type,
        item_keys=item_keys,
        full_text=args.full,
    )


if __name__ == "__main__":
    main()
