"""
Evidence-Gated Hybrid & Curated Paper Relations Engine.

Discovers, verifies, and transactionally persists high-confidence undirected edges
between academic papers in conversion_meta.db without LLMs or cross-encoders:
1. Sub-millisecond 5-feature statistical keywords and Zotero user tags.
2. Chunk-level hybrid search (Chroma dense + SQLite FTS5 BM25 with RRF).
3. The Canonical Evidence Gate:
   - Method == 'hybrid' (dense semantic + BM25 keyword corroboration), OR
   - High single-chunk semantic similarity >= 0.65, OR
   - Multi-chunk dense cohesion (>= 2 chunks and max_sem >= 0.58).
4. Exponential diminishing-returns score rescaling to intuitive [0.50, 0.99].
5. Ground truth ingestion (human_curated, shared_tags, human_curated_and_hybrid_search).
6. Undirected edge symmetrization with canonical max score and ON CONFLICT semantics.
"""

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zocrates.config import (
    DEFAULT_GRAPH_RELATIONS,
    get_corpus_dir,
    get_graph_relations_config,
)
from zocrates.rag.keyword_extractor import (
    GENERIC_TAGS,
    build_search_query,
    extract_statistical_keywords,
    extract_structural_digest,
    get_item_tags,
)

logger = logging.getLogger(__name__)


def rescale_hybrid_score(
    raw_rrf_score: float,
    min_hybrid_score: float = 0.015,
    score_tau: float = 0.10,
) -> float:
    """Rescale raw aggregated RRF score to [0.50, 0.99] using exponential diminishing returns.

    Formula:
        score = round(0.50 + 0.49 * (1.0 - exp(-(raw_rrf_score - min_hybrid_score) / score_tau)), 2)
    Clamped strictly to [0.50, 0.99].

    Args:
        raw_rrf_score: Aggregated raw Reciprocal Rank Fusion score.
        min_hybrid_score: Minimum raw score threshold to qualify.
        score_tau: Exponential decay constant governing progression.

    Returns:
        Rescaled confidence score in [0.50, 0.99] rounded to 2 decimals.
    """
    if score_tau <= 0:
        score_tau = 0.10

    diff = raw_rrf_score - min_hybrid_score
    scaled = 0.50 + 0.49 * (1.0 - math.exp(-diff / score_tau))
    clamped = max(0.50, min(0.99, scaled))
    return round(clamped, 2)


def check_evidence_gate(
    chunks: list[Any],
    min_hybrid_score: float = 0.015,
) -> tuple[bool, float, float, set[str]]:
    """Check if retrieved chunks satisfy the canonical Evidence Gate.

    A candidate paper connection clears the Evidence Gate if:
    agg_score >= min_hybrid_score AND at least one of:
    1. Any chunk has retrieval_method == 'hybrid' (lexical BM25 + dense cos-sim >= 0.50).
    2. High single-chunk semantic confidence: max_sem_score >= 0.65.
    3. Multi-chunk dense cohesion: len(chunks) >= 2 and max_sem_score >= 0.58.

    Args:
        chunks: List of retrieved SearchResult chunks or dict equivalents.
        min_hybrid_score: Minimum aggregated raw RRF score threshold.

    Returns:
        Tuple of (is_valid, agg_score, max_sem_score, retrieval_methods).
    """
    if not chunks:
        return False, 0.0, 0.0, set()

    agg_score = 0.0
    sem_scores: list[float] = []
    methods: set[str] = set()

    for c in chunks:
        if isinstance(c, dict):
            s = float(c.get("score") or 0.0)
            raw_sem = c.get("raw_semantic_score")
            method = str(c.get("retrieval_method") or "unknown")
        else:
            s = float(getattr(c, "score", 0.0) or 0.0)
            raw_sem = getattr(c, "raw_semantic_score", None)
            method = str(getattr(c, "retrieval_method", "unknown") or "unknown")

        agg_score += s
        methods.add(method)
        if raw_sem is not None:
            sem_scores.append(float(raw_sem))

    max_sem_score = max(sem_scores) if sem_scores else 0.0
    has_hybrid = "hybrid" in methods

    # Multi-chunk cohesion requires at least 2 distinct/independent chunks
    chunk_identifiers = set()
    for c in chunks:
        cid = c.get("chunk_id") if isinstance(c, dict) else getattr(c, "chunk_id", None)
        if not cid:
            cid = c.get("chunk_index") if isinstance(c, dict) else getattr(c, "chunk_index", None)
        if not cid:
            cid = id(c)
        chunk_identifiers.add(cid)
    is_multi_chunk = len(chunk_identifiers) >= 2 and max_sem_score >= 0.58

    corroborated = (
        has_hybrid
        or max_sem_score >= 0.65
        or is_multi_chunk
    )

    is_valid = bool(agg_score >= min_hybrid_score and corroborated)
    return is_valid, agg_score, max_sem_score, methods


def upsert_edge(
    conn: sqlite3.Connection,
    source_key: str,
    target_key: str,
    score: float,
    relation_type: str,
    match_evidence: str | None = None,
    symmetrize: bool = True,
) -> None:
    """Insert or update an edge in SQLite item_relations with max-score semantics.

    Always enforces undirected edge symmetrization by persisting both (A, B)
    and (B, A) with the canonical max score when symmetrize is True.

    Args:
        conn: Open SQLite connection to conversion_meta.db.
        source_key: Source item or attachment key.
        target_key: Target item or attachment key.
        score: Edge confidence score in [0.50, 1.0].
        relation_type: Relation category ('human_curated_and_hybrid_search',
            'human_curated', 'hybrid_search', or 'shared_tags').
        match_evidence: Optional JSON evidence debug summary string.
        symmetrize: If True, writes both directional rows with canonical max score.
    """
    if source_key == target_key:
        return

    conn.execute("PRAGMA busy_timeout = 5000;")
    updated_at = datetime.now(UTC).isoformat()

    if symmetrize:
        # Determine canonical max score across existing forward and backward rows
        existing_scores = [score]
        try:
            cursor = conn.execute(
                """
                SELECT score FROM item_relations
                WHERE (source_key = ? AND target_key = ? AND relation_type = ?)
                   OR (source_key = ? AND target_key = ? AND relation_type = ?)
                """,
                (source_key, target_key, relation_type, target_key, source_key, relation_type),
            )
            for row in cursor.fetchall():
                existing_scores.append(float(row["score"] if isinstance(row, sqlite3.Row) else row[0]))
        except Exception:
            pass

        canonical_score = max(existing_scores)
        pairs = [(source_key, target_key), (target_key, source_key)]
    else:
        canonical_score = score
        pairs = [(source_key, target_key)]

    sql = """
        INSERT INTO item_relations (
            source_key, target_key, score, relation_type, match_evidence, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key, target_key, relation_type) DO UPDATE SET
            score = max(item_relations.score, excluded.score),
            match_evidence = excluded.match_evidence,
            updated_at = excluded.updated_at;
    """
    with conn:
        for src, dst in pairs:
            conn.execute(
                sql,
                (src, dst, canonical_score, relation_type, match_evidence, updated_at),
            )


def sync_manual_relations(
    conn: sqlite3.Connection,
    zotero_dir: Path,
    target_key: str | None = None,
) -> int:
    """Read itemRelations from zotero.sqlite and persist human_curated edges.

    If a manual link matches an existing hybrid_search edge, promotes it to
    human_curated_and_hybrid_search with score 1.0 while preserving underlying
    evidence. Otherwise, inserts human_curated with score 1.0.

    Args:
        conn: Open SQLite connection to conversion_meta.db.
        zotero_dir: Root Zotero data directory containing zotero.sqlite.
        target_key: Optional filter to restrict sync to relations involving this key.

    Returns:
        Number of manual edges processed and persisted.
    """
    sqlite_path = zotero_dir / "zotero.sqlite"
    if not sqlite_path.exists():
        return 0

    # Build key mapping from conversion_meta
    key_map: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT item_key, parent_item_key, content_type FROM conversion_meta"
        ).fetchall()
        for r in rows:
            k = r["item_key"]
            pk = r["parent_item_key"]
            is_pdf = r["content_type"] == "application/pdf"
            key_map[k] = k
            if pk:
                if pk not in key_map or is_pdf:
                    key_map[pk] = k
    except Exception:
        pass

    target_parent = None
    if target_key:
        try:
            p_row = conn.execute(
                "SELECT parent_item_key FROM conversion_meta WHERE item_key = ?",
                (target_key,),
            ).fetchone()
            if p_row and p_row["parent_item_key"]:
                target_parent = p_row["parent_item_key"]
        except Exception:
            pass

    synced_count = 0
    try:
        from zocrates.rag.zotero_db import _open_db

        with _open_db(sqlite_path) as z_conn:
            has_deleted_items = bool(
                z_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deletedItems'"
                ).fetchone()
            )
            del_clause = "WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT i.key AS source_key, ir.object AS target_uri
                FROM itemRelations ir
                JOIN items i ON ir.itemID = i.itemID
                {del_clause}
            """
            manual_rows = z_conn.execute(sql).fetchall()

            for r in manual_rows:
                raw_src = str(r["source_key"]).strip()
                raw_uri = str(r["target_uri"]).strip()
                if "/items/" not in raw_uri:
                    continue

                raw_dest = raw_uri.split("/items/")[-1].strip()
                if not raw_dest:
                    continue

                # Filter by target_key if requested
                if target_key:
                    matches_src = raw_src in (target_key, target_parent)
                    matches_dst = raw_dest in (target_key, target_parent)
                    if not (matches_src or matches_dst):
                        continue

                eff_src = key_map.get(raw_src, raw_src)
                eff_dst = key_map.get(raw_dest, raw_dest)
                if eff_src == eff_dst:
                    continue

                # Check if edge already exists for this pair
                existing_hybrid = None
                already_dual = False
                try:
                    ex_dual = conn.execute(
                        """
                        SELECT 1 FROM item_relations
                        WHERE source_key = ? AND target_key = ? AND relation_type = 'human_curated_and_hybrid_search'
                        """,
                        (eff_src, eff_dst),
                    ).fetchone()
                    if ex_dual:
                        already_dual = True
                    else:
                        ex_row = conn.execute(
                            """
                            SELECT match_evidence FROM item_relations
                            WHERE source_key = ? AND target_key = ? AND relation_type = 'hybrid_search'
                            """,
                            (eff_src, eff_dst),
                        ).fetchone()
                        if ex_row:
                            existing_hybrid = ex_row["match_evidence"]
                except Exception:
                    pass

                if already_dual:
                    continue

                if existing_hybrid is not None:
                    rel_type = "human_curated_and_hybrid_search"
                    match_evidence = existing_hybrid
                else:
                    rel_type = "human_curated"
                    match_evidence = json.dumps({"source": "zotero_itemRelations"})

                upsert_edge(
                    conn=conn,
                    source_key=eff_src,
                    target_key=eff_dst,
                    score=1.0,
                    relation_type=rel_type,
                    match_evidence=match_evidence,
                    symmetrize=True,
                )
                synced_count += 1
    except Exception as exc:
        logger.warning("Failed to sync manual relations from %s: %s", sqlite_path, exc)

    return synced_count


def sync_shared_tags(
    conn: sqlite3.Connection,
    zotero_dir: Path,
    min_shared_tags: int = 2,
    target_key: str | None = None,
    score: float | None = None,
) -> int:
    """Check for shared user tags (type=0) and persist shared_tags edges.

    Requires at least min_shared_tags (default: 2) distinctive non-generic user tags.

    Args:
        conn: Open SQLite connection to conversion_meta.db.
        zotero_dir: Root Zotero data directory containing zotero.sqlite.
        min_shared_tags: Minimum count of shared distinctive tags required.
        target_key: Optional filter to restrict comparison to this item key.
        score: Optional score override (defaults to config shared_tags_score or 0.70).

    Returns:
        Number of shared_tags edges created.
    """
    sqlite_path = zotero_dir / "zotero.sqlite"
    if not sqlite_path.exists():
        return 0

    effective_score = score
    cfg_graph = None
    try:
        from zocrates.config import get_graph_relations_config
        cfg_graph = get_graph_relations_config(zotero_dir)
    except Exception:
        pass

    if effective_score is None:
        effective_score = float(cfg_graph.get("shared_tags_score", 0.70)) if cfg_graph else 0.70

    include_auto = cfg_graph.get("include_automatic_tags_in_shared_tags", True) if cfg_graph else True
    tag_type = None if include_auto else 0

    try:
        rows = conn.execute(
            "SELECT item_key, parent_item_key, content_type FROM conversion_meta"
        ).fetchall()
    except Exception:
        return 0

    if not rows:
        return 0

    # Resolve primary bundle key per parent paper and exclude child notes from tag entity map
    parent_map: dict[str, str] = {}
    paper_primary_key: dict[str, str] = {}

    for r in rows:
        k = r["item_key"]
        pk = r["parent_item_key"]
        ct = r["content_type"]
        is_pdf = ct == "application/pdf"
        parent_id = pk or k
        parent_map[k] = parent_id

        # Skip child notes attached to a parent from being distinct tag sharing entities
        if ct == "text/note" and pk:
            continue

        # Choose primary bundle key for each parent paper (prefer PDF)
        if parent_id not in paper_primary_key or is_pdf:
            paper_primary_key[parent_id] = k

    # Retrieve tags per unique parent paper
    unique_paper_tags: dict[str, set[str]] = {}
    for parent_id, primary_k in paper_primary_key.items():
        tags = set(get_item_tags(zotero_dir, item_key=parent_id, tag_type=tag_type))
        if not tags and primary_k != parent_id:
            tags = set(get_item_tags(zotero_dir, item_key=primary_k, tag_type=tag_type))
        unique_paper_tags[parent_id] = tags

    # Compute Document Frequency (DF) across unique parent papers in the library
    tag_df: dict[str, int] = {}
    for tags in unique_paper_tags.values():
        for t in tags:
            tag_df[t] = tag_df.get(t, 0) + 1

    total_items = len(unique_paper_tags)
    df_cap = max(5, int(total_items * 0.05)) if total_items > 0 else 5

    item_tags_map: dict[str, set[str]] = {}
    for parent_id, tags in unique_paper_tags.items():
        primary_k = paper_primary_key.get(parent_id)
        if primary_k:
            item_tags_map[primary_k] = {t for t in tags if tag_df[t] <= df_cap}

    created_count = 0
    if target_key:
        target_bundle_key = target_key
        target_parent = parent_map.get(target_key, target_key)
        if target_key in paper_primary_key:
            target_bundle_key = paper_primary_key[target_key]

        src_tags = item_tags_map.get(target_bundle_key, set())
        if not src_tags:
            return 0
        for other_key, other_tags in item_tags_map.items():
            if other_key == target_bundle_key or parent_map.get(other_key) == target_parent:
                continue
            shared = src_tags & other_tags
            if len(shared) >= min_shared_tags:
                evidence = json.dumps({"shared_tags": sorted(list(shared))})
                upsert_edge(
                    conn=conn,
                    source_key=target_bundle_key,
                    target_key=other_key,
                    score=effective_score,
                    relation_type="shared_tags",
                    match_evidence=evidence,
                    symmetrize=True,
                )
                created_count += 1
    else:
        all_keys = sorted(item_tags_map.keys())
        for i in range(len(all_keys)):
            k1 = all_keys[i]
            p1 = parent_map.get(k1, k1)
            t1 = item_tags_map[k1]
            if len(t1) < min_shared_tags:
                continue
            for j in range(i + 1, len(all_keys)):
                k2 = all_keys[j]
                p2 = parent_map.get(k2, k2)
                if p1 == p2:
                    continue
                t2 = item_tags_map[k2]
                shared = t1 & t2
                if len(shared) >= min_shared_tags:
                    evidence = json.dumps({"shared_tags": sorted(list(shared))})
                    upsert_edge(
                        conn=conn,
                        source_key=k1,
                        target_key=k2,
                        score=effective_score,
                        relation_type="shared_tags",
                        match_evidence=evidence,
                        symmetrize=True,
                    )
                    created_count += 1

    return created_count


def evaluate_and_persist_edges(
    item_key: str,
    syncer: Any,
    sync_bundle_files: bool = True,
    sync_ground_truth: bool = True,
) -> list[dict[str, Any]]:
    """Evaluate and persist relations for an item using hybrid search and ground truth.

    1. Looks up item_key metadata, keywords, and tags.
    2. Builds high-signal search query via build_search_query.
    3. Runs syncer.store.search isolating body chunks and excluding Chunk 0 frontmatter.
    4. Groups candidates by parent key, strictly excluding own parent key and sibling attachments.
    5. Clears candidates through the canonical Evidence Gate and exponential score rescaling.
    6. Persists edges transactionally with ON CONFLICT max score and undirected symmetrization.
    7. Syncs manual links and shared user tags if enabled.
    8. If sync_bundle_files is True, regenerates item_relations.md for item_key and all connected neighbors.

    Args:
        item_key: Unique Zotero attachment or item key.
        syncer: Active LibrarySyncer instance with vector store and database.
        sync_bundle_files: If True, regenerates item_relations.md on disk.

    Returns:
        List of all verified relation dictionaries for item_key.
    """
    zotero_dir = getattr(syncer, "_zotero_data_dir", None) or getattr(syncer, "zotero_dir", None)
    if zotero_dir:
        zotero_dir = Path(zotero_dir)

    processor = getattr(syncer, "processor", None)
    cache_dir = getattr(processor, "_cache_dir", None) or (get_corpus_dir(zotero_dir) if zotero_dir else None)
    if not cache_dir:
        return []

    cache_dir = Path(cache_dir)
    meta_db_path = cache_dir / "conversion_meta.db"
    if not meta_db_path.exists():
        return []

    cfg = get_graph_relations_config(zotero_dir)
    if not cfg.get("enabled", True):
        return []

    conn = sqlite3.connect(str(meta_db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    try:
        item_row = conn.execute(
            """
            SELECT item_key, parent_item_key, title, abstract, keywords, content_type, abstract_source
            FROM conversion_meta
            WHERE item_key = ?
            """,
            (item_key,),
        ).fetchone()

        if not item_row:
            return []

        col_keys = item_row.keys()
        is_metadata_bundle = item_row["content_type"] == "text/zotero-metadata"
        abs_src = item_row["abstract_source"] if "abstract_source" in col_keys else None
        has_substantive_abstract = bool(item_row["abstract"] and abs_src != "none")

        # If an item has NO abstract, skip running dense/BM25 hybrid search
        if is_metadata_bundle and not has_substantive_abstract:
            if sync_ground_truth:
                if cfg.get("include_human_curated", True) and zotero_dir:
                    sync_manual_relations(conn=conn, zotero_dir=zotero_dir, target_key=item_key)
                if cfg.get("include_shared_tags", True) and zotero_dir:
                    sync_shared_tags(
                        conn=conn,
                        zotero_dir=zotero_dir,
                        min_shared_tags=int(cfg.get("min_shared_tags", 2)),
                        target_key=item_key,
                        score=float(cfg.get("shared_tags_score", 0.70)),
                    )
            edges_cursor = conn.execute(
                """
                SELECT source_key, target_key, score, relation_type, match_evidence, updated_at
                FROM item_relations
                WHERE source_key = ?
                ORDER BY score DESC, target_key ASC
                """,
                (item_key,),
            )
            persisted_edges = [dict(r) for r in edges_cursor.fetchall()]
            if sync_bundle_files and cfg.get("sync_bundle_files", True):
                try:
                    from zocrates.rag.bundle_catalog import regenerate_item_relations_md

                    regenerate_item_relations_md(item_key=item_key, cache_dir=cache_dir)
                    connected_neighbors = {
                        e["target_key"] for e in persisted_edges if e.get("target_key")
                    }
                    for neighbor_key in connected_neighbors:
                        regenerate_item_relations_md(item_key=neighbor_key, cache_dir=cache_dir)
                except Exception as exc:
                    logger.warning(
                        "Failed to synchronize item_relations.md files for %s: %s",
                        item_key,
                        exc,
                    )
            return persisted_edges

        title = str(item_row["title"] or "").strip()
        abstract = str(item_row["abstract"] or "").strip()
        keywords: list[str] = []

        if item_row["keywords"]:
            try:
                parsed_kw = json.loads(item_row["keywords"])
                if isinstance(parsed_kw, list):
                    keywords = [str(k) for k in parsed_kw]
            except Exception:
                keywords = []

        if not keywords:
            # Fall back to extracting keywords from document.md
            doc_path = cache_dir / item_key / "document.md"
            if doc_path.exists():
                try:
                    doc_text = doc_path.read_text(encoding="utf-8", errors="ignore")
                    digest = extract_structural_digest(doc_text)
                    keywords = extract_statistical_keywords(title, digest, max_keywords=6)
                except Exception:
                    pass
            if not keywords and abstract:
                keywords = extract_statistical_keywords(title, abstract, max_keywords=6)

        tags = get_item_tags(zotero_dir, item_key=item_key) if zotero_dir else []
        query = build_search_query(title=title, keywords=keywords, tags=tags)

        # Build mapping for parent isolation and primary bundle resolution
        all_meta_rows = conn.execute(
            "SELECT item_key, parent_item_key, content_type FROM conversion_meta"
        ).fetchall()

        parent_map: dict[str, str] = {}
        parent_to_bundle: dict[str, str] = {}

        for r in all_meta_rows:
            k = r["item_key"]
            pk = r["parent_item_key"] or k
            is_pdf = r["content_type"] == "application/pdf"
            parent_map[k] = pk
            if pk not in parent_to_bundle or is_pdf:
                parent_to_bundle[pk] = k

        source_parent_key = parent_map.get(item_key, item_key)

        # Hybrid search edge discovery
        strategy = cfg.get("strategy", "hybrid_corroborated")
        if strategy != "curated_only" and query and len(query.strip()) >= 3 and hasattr(syncer, "store"):
            candidate_pool_size = int(cfg.get("candidate_pool_size", 50))
            min_hybrid_score = float(cfg.get("min_hybrid_score", 0.015))
            score_tau = float(cfg.get("score_tau", 0.10))

            try:
                search_results = syncer.store.search(
                    query=query,
                    top_k=candidate_pool_size,
                    final_k=candidate_pool_size,
                    chunk_type_filter=["text", "table", "equation", "code", "figure"],
                )
            except Exception as exc:
                logger.warning("Graph hybrid search failed for %s: %s", item_key, exc)
                search_results = []

            # Group chunks by target parent key, strictly excluding own parent and sibling attachments
            by_target: dict[str, list[Any]] = {}
            for chunk in search_results:
                chunk_key = chunk.item_key if hasattr(chunk, "item_key") else chunk.get("item_key")
                target_parent = parent_map.get(chunk_key, chunk_key)

                # Strict sibling guard: exclude self-matches, child notes, and companion attachments
                if target_parent == source_parent_key or chunk_key == item_key:
                    continue

                by_target.setdefault(target_parent, []).append(chunk)

            # Evaluate each candidate group against the Evidence Gate
            for target_parent, chunks in by_target.items():
                is_valid, agg_score, max_sem, methods = check_evidence_gate(
                    chunks, min_hybrid_score=min_hybrid_score
                )
                if not is_valid:
                    continue

                target_bundle_key = parent_to_bundle.get(target_parent, target_parent)
                rescaled_score = rescale_hybrid_score(
                    raw_rrf_score=agg_score,
                    min_hybrid_score=min_hybrid_score,
                    score_tau=score_tau,
                )

                # Check if manual link exists to determine promotion
                is_manual_corroborated = False
                if cfg.get("include_human_curated", True) and zotero_dir:
                    try:
                        from zocrates.rag.zotero_db import _open_db

                        sqlite_path = zotero_dir / "zotero.sqlite"
                        if sqlite_path.exists():
                            with _open_db(sqlite_path) as z_conn:
                                q = """
                                    SELECT 1 FROM itemRelations ir
                                    JOIN items i ON ir.itemID = i.itemID
                                    WHERE (i.key = ? AND ir.object LIKE '%/items/' || ?)
                                       OR (i.key = ? AND ir.object LIKE '%/items/' || ?)
                                """
                                row = z_conn.execute(
                                    q,
                                    (source_parent_key, target_parent, target_parent, source_parent_key),
                                ).fetchone()
                                if row:
                                    is_manual_corroborated = True
                    except Exception:
                        pass

                if is_manual_corroborated:
                    rel_type = "human_curated_and_hybrid_search"
                    final_score = 1.0
                else:
                    rel_type = "hybrid_search"
                    final_score = rescaled_score

                match_evidence = json.dumps(
                    {
                        "raw_rrf_score": round(agg_score, 4),
                        "matched_chunks": len(chunks),
                        "max_semantic_score": round(max_sem, 4),
                        "retrieval_methods": sorted(list(methods)),
                    }
                )

                upsert_edge(
                    conn=conn,
                    source_key=item_key,
                    target_key=target_bundle_key,
                    score=final_score,
                    relation_type=rel_type,
                    match_evidence=match_evidence,
                    symmetrize=True,
                )

        # Ground truth ingestion: manual links & shared tags
        if sync_ground_truth:
            if cfg.get("include_human_curated", True) and zotero_dir:
                sync_manual_relations(conn=conn, zotero_dir=zotero_dir, target_key=item_key)

            if cfg.get("include_shared_tags", True) and zotero_dir:
                sync_shared_tags(
                    conn=conn,
                    zotero_dir=zotero_dir,
                    min_shared_tags=int(cfg.get("min_shared_tags", 2)),
                    target_key=item_key,
                    score=float(cfg.get("shared_tags_score", 0.70)),
                )

        # Query all verified edges for item_key
        edges_cursor = conn.execute(
            """
            SELECT source_key, target_key, score, relation_type, match_evidence, updated_at
            FROM item_relations
            WHERE source_key = ?
            ORDER BY score DESC, target_key ASC
            """,
            (item_key,),
        )
        persisted_edges = [dict(r) for r in edges_cursor.fetchall()]

        # Atomic bundle file synchronization
        if sync_bundle_files and cfg.get("sync_bundle_files", True):
            try:
                from zocrates.rag.bundle_catalog import regenerate_item_relations_md

                # Regenerate for item_key
                regenerate_item_relations_md(item_key=item_key, cache_dir=cache_dir)

                # Regenerate for all connected neighbors
                connected_neighbors = {e["target_key"] for e in persisted_edges if e.get("target_key")}
                for neighbor_key in connected_neighbors:
                    regenerate_item_relations_md(item_key=neighbor_key, cache_dir=cache_dir)
            except Exception as exc:
                logger.warning(
                    "Failed to synchronize item_relations.md files for %s: %s", item_key, exc
                )

        return persisted_edges
    finally:
        conn.close()
