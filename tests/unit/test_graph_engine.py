"""
Unit tests for Graph Paper Relations Engine, Keywords & Config Foundation (Phase 1).

Verifies:
1. Config loading, defaults, and disk backfilling of graph_relations.
2. Keyword extraction quality, sub-millisecond latency (< 1 ms), stopword/header filtering, and tag fetching.
3. Search query deduplication.
4. Score rescaling curve (boundaries, tau parameter, clamping, monotonicity).
5. Canonical Evidence Gate conditions and rejection of sub-threshold candidates.
6. Edge upserting, undirected symmetrization, and ON CONFLICT max-score semantics.
7. Ground truth ingestion (manual links promotion, shared tags detection).
8. evaluate_and_persist_edges end-to-end with sibling attachment isolation.
"""

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.config import (
    DEFAULT_GRAPH_RELATIONS,
    get_config_path,
    get_graph_relations_config,
    load_zocrates_config,
)
from zocrates.rag.document_processor import _open_meta_db
from zocrates.rag.graph_engine import (
    check_evidence_gate,
    evaluate_and_persist_edges,
    rescale_hybrid_score,
    sync_manual_relations,
    sync_shared_tags,
    upsert_edge,
)
from zocrates.rag.keyword_extractor import (
    GENERIC_SECTION_HEADERS,
    GENERIC_TAGS,
    STOPWORDS,
    build_search_query,
    clean_markdown_for_kw,
    extract_statistical_keywords,
    extract_structural_digest,
    get_item_tags,
    is_generic_section_header,
)
from zocrates.rag.vector_store import SearchResult


# ---------------------------------------------------------------------------
# 1. Config Loading, Defaults & Backfilling
# ---------------------------------------------------------------------------


def test_graph_relations_defaults(tmp_path: Path):
    """Test that get_graph_relations_config returns DEFAULT_GRAPH_RELATIONS."""
    cfg = get_graph_relations_config(tmp_path)
    assert cfg["enabled"] is True
    assert cfg["strategy"] == "hybrid_corroborated"
    assert cfg["candidate_pool_size"] == 50
    assert cfg["min_hybrid_score"] == 0.015
    assert cfg["score_tau"] == 0.10
    assert cfg["shared_tags_score"] == 0.70
    assert cfg["include_human_curated"] is True
    assert cfg["include_shared_tags"] is True
    assert cfg["min_shared_tags"] == 2
    assert cfg["sync_bundle_files"] is True


def test_graph_relations_backfilling(tmp_path: Path):
    """Test that existing config files on disk missing graph_relations are backfilled under lock."""
    config_path = get_config_path(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a legacy config file without graph_relations
    with open(config_path, "w") as f:
        json.dump({"llm_provider": "openrouter_llm", "llm_model": "google/gemini-3.8-flash"}, f)

    # Verify disk does not have graph_relations
    with open(config_path) as f:
        disk_raw = json.load(f)
    assert "graph_relations" not in disk_raw

    # Load config - should backfill to disk
    loaded = load_zocrates_config(tmp_path)
    assert "graph_relations" in loaded
    assert loaded["graph_relations"] == DEFAULT_GRAPH_RELATIONS

    # Verify disk now has the backfilled block
    with open(config_path) as f:
        disk_updated = json.load(f)
    assert "graph_relations" in disk_updated
    assert disk_updated["graph_relations"] == DEFAULT_GRAPH_RELATIONS


# ---------------------------------------------------------------------------
# 2. Keyword Extraction & Structural Digest
# ---------------------------------------------------------------------------


def test_clean_markdown_for_kw():
    """Verify clean_markdown_for_kw strips code, URLs, LaTeX, links, and HTML."""
    raw = (
        "Here is code: ```python\nx = 1\n``` and inline `import os`. "
        "Math: $$E = mc^2$$ and $y = mx + b$. "
        "Link: [Open Knowledge Format](https://example.com/okf) and bare https://example.com. "
        "HTML: <span class=\"highlight\">provenance</span>."
    )
    cleaned = clean_markdown_for_kw(raw)
    assert "x = 1" not in cleaned
    assert "import os" not in cleaned
    assert "E = mc^2" not in cleaned
    assert "y = mx + b" not in cleaned
    assert "https://example.com" not in cleaned
    assert "<span" not in cleaned
    assert "Open Knowledge Format" in cleaned
    assert "provenance" in cleaned


def test_generic_section_header_detection():
    """Verify generic IMRAD section headers are detected and filtered."""
    assert is_generic_section_header("1. Introduction") is True
    assert is_generic_section_header("## Methods") is True
    assert is_generic_section_header("### Results and Discussion") is True
    assert is_generic_section_header("References") is True
    assert is_generic_section_header("Bibliography") is True
    assert is_generic_section_header("Appendix") is True

    # Topical substantive headers must NOT be flagged as generic
    assert is_generic_section_header("### Scaled Dot-Product Attention") is False
    assert is_generic_section_header("## Neural Retrieval Optimization") is False
    assert is_generic_section_header("### Whole-Genome Sequencing and Variant Calling") is False


def test_extract_structural_digest():
    """Verify extract_structural_digest extracts lead paragraphs and substantive headers."""
    doc = """---
title: Test
---
# Introduction
In this study, we propose a new framework. The first two sentences form the lead.

## Methods
We used standard approaches.

### Dual-Channel Retrieval Architecture
Our system coordinates dense Chroma vectors with sparse FTS5 BM25 indexes.

### Empirical Evidence Gating
Matches are validated against the three canonical corroboration paths.

## Conclusion
The results confirm the efficacy.
"""
    digest = extract_structural_digest(doc, max_lead=200)
    assert "Introduction" not in digest.split("\n\n")[-1]  # Header block shouldn't have Introduction
    assert "Methods" not in digest.split("\n\n")[-1]
    assert "Dual-Channel Retrieval Architecture" in digest
    assert "Empirical Evidence Gating" in digest
    assert "first two sentences" in digest


def test_extract_statistical_keywords_quality_and_speed():
    """Verify 5-feature statistical keywords extract high-signal phrases in < 1 ms."""
    title = "Open Knowledge Format: Securing Trust Signals and Provenance in Academic Markdown Bundles"
    text = """
    Data provenance in scientific computing is critical. We introduce the Open Knowledge Format (OKF).
    OKF bundles establish verifiable trust signals and cryptographic hash digests for research artifacts.
    Experimental evaluations demonstrate that immutable metadata sidecars prevent downstream hallucination.
    """
    digest = extract_structural_digest(text)

    # Test speed across multiple runs
    t0 = time.perf_counter()
    for _ in range(50):
        keywords = extract_statistical_keywords(title, digest, max_keywords=6)
    elapsed = (time.perf_counter() - t0) / 50

    # Sub-millisecond latency requirement (< 1 ms; typically ~0.15-0.20 ms)
    assert elapsed < 0.005, f"Keyword extraction too slow: {elapsed*1000:.2f} ms"

    assert len(keywords) > 0
    assert len(keywords) <= 6

    # Verify academic stopwords and boilerplate are NOT keywords
    stopwords_lower = {w.lower() for w in STOPWORDS}
    for kw in keywords:
        for word in kw.split():
            assert word.lower() not in stopwords_lower or len(kw.split()) > 1

    # Check that high-signal domain phrases were captured
    joined_kw = " ".join(keywords).lower()
    assert any(term in joined_kw for term in ["open knowledge format", "trust signals", "provenance", "okf"])


def test_build_search_query_deduplication():
    """Verify build_search_query produces clean, deduplicated queries."""
    title = "Deep Learning for Computer Vision"
    keywords = ["deep learning", "convolutional neural network", "vision transformer"]
    tags = ["cs.CV", "deep learning", "neural networks"]

    query = build_search_query(title, keywords, tags)
    words = query.split()

    # Case-insensitive word frequency should be 1 for each term
    lower_words = [w.lower() for w in words]
    assert len(lower_words) == len(set(lower_words))

    # All primary terms should be present
    assert "deep" in lower_words
    assert "learning" in lower_words
    assert "vision" in lower_words
    assert "convolutional" in lower_words
    assert "transformer" in lower_words


def test_get_item_tags(tmp_path: Path):
    """Verify get_item_tags queries zotero.sqlite, filters generic tags, and maps attachments."""
    zotero_dir = tmp_path / "Zotero"
    zotero_dir.mkdir(parents=True)
    sqlite_path = zotero_dir / "zotero.sqlite"

    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
            CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
            CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER);

            -- Parent item
            INSERT INTO items (itemID, key) VALUES (1, 'PARENT01');
            -- Child attachment
            INSERT INTO items (itemID, key) VALUES (2, 'ATTACH01');
            INSERT INTO itemAttachments (itemID, parentItemID) VALUES (2, 1);

            -- Tags
            INSERT INTO tags (tagID, name) VALUES (10, 'cs.AI');
            INSERT INTO tags (tagID, name) VALUES (11, 'Graph Relations');
            INSERT INTO tags (tagID, name) VALUES (12, 'to-read');
            INSERT INTO tags (tagID, name) VALUES (13, 'favorite');

            -- Link tags to parent item
            INSERT INTO itemTags (itemID, tagID) VALUES (1, 10);
            INSERT INTO itemTags (itemID, tagID) VALUES (1, 11);
            INSERT INTO itemTags (itemID, tagID) VALUES (1, 12);
            INSERT INTO itemTags (itemID, tagID) VALUES (1, 13);
            """
        )

    # Querying parent key directly
    parent_tags = get_item_tags(zotero_dir, item_key="PARENT01")
    assert "cs.AI" in parent_tags
    assert "Graph Relations" in parent_tags
    assert "to-read" not in parent_tags
    assert "favorite" not in parent_tags

    # Querying child attachment key should resolve parent tags
    child_tags = get_item_tags(zotero_dir, item_key="ATTACH01")
    assert "cs.AI" in child_tags
    assert "Graph Relations" in child_tags
    assert "to-read" not in child_tags


# ---------------------------------------------------------------------------
# 3. Score Rescaling Curve
# ---------------------------------------------------------------------------


def test_rescale_hybrid_score_boundaries_and_monotonicity():
    """Verify exponential score rescaling curve boundaries, monotonicity, and tau effect."""
    min_score = 0.015
    tau = 0.10

    # At exactly min_score, score should be 0.50
    s_boundary = rescale_hybrid_score(min_score, min_hybrid_score=min_score, score_tau=tau)
    assert s_boundary == 0.50

    # Below min_score, clamps to 0.50
    s_below = rescale_hybrid_score(0.005, min_hybrid_score=min_score, score_tau=tau)
    assert s_below == 0.50

    # Incremental scores should be strictly monotonically non-decreasing
    test_scores = [0.015, 0.025, 0.05, 0.08, 0.15, 0.30, 0.60, 1.20, 5.00]
    rescaled = [
        rescale_hybrid_score(s, min_hybrid_score=min_score, score_tau=tau)
        for s in test_scores
    ]

    for i in range(len(rescaled) - 1):
        assert rescaled[i] <= rescaled[i + 1], f"Monotonicity failed between {rescaled[i]} and {rescaled[i+1]}"

    # Large values should asymptotically approach 0.99 without exceeding it
    assert rescaled[-1] == 0.99
    assert all(0.50 <= s <= 0.99 for s in rescaled)

    # Check realistic empirical milestones from plan
    s_2chunks = rescale_hybrid_score(0.045, min_hybrid_score=0.015, score_tau=0.10)
    assert 0.60 <= s_2chunks <= 0.66

    s_dense = rescale_hybrid_score(0.15, min_hybrid_score=0.015, score_tau=0.10)
    assert 0.85 <= s_dense <= 0.92


# ---------------------------------------------------------------------------
# 4. Canonical Evidence Gate
# ---------------------------------------------------------------------------


def test_evidence_gate_condition_1_hybrid_method():
    """Evidence Gate: Clears when at least one chunk has retrieval_method == 'hybrid'."""
    chunks = [
        SearchResult(
            chunk_text="corroborated text",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=1,
            score=0.020,
            raw_semantic_score=0.52,
            retrieval_method="hybrid",
        )
    ]
    is_valid, agg_score, max_sem, methods = check_evidence_gate(chunks, min_hybrid_score=0.015)
    assert is_valid is True
    assert agg_score == 0.020
    assert max_sem == 0.52
    assert "hybrid" in methods


def test_evidence_gate_condition_2_high_single_chunk_semantic():
    """Evidence Gate: Clears when a single chunk has raw_semantic_score >= 0.65."""
    chunks = [
        SearchResult(
            chunk_text="high semantic similarity chunk",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=1,
            score=0.018,
            raw_semantic_score=0.66,
            retrieval_method="semantic",
        )
    ]
    is_valid, agg_score, max_sem, methods = check_evidence_gate(chunks, min_hybrid_score=0.015)
    assert is_valid is True
    assert agg_score == 0.018
    assert max_sem == 0.66
    assert "hybrid" not in methods


def test_evidence_gate_condition_3_multi_chunk_semantic():
    """Evidence Gate: Clears when >= 2 chunks have max_sem_score >= 0.58."""
    chunks = [
        SearchResult(
            chunk_text="chunk 1",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=1,
            score=0.010,
            raw_semantic_score=0.59,
            retrieval_method="semantic",
        ),
        SearchResult(
            chunk_text="chunk 2",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=2,
            score=0.012,
            raw_semantic_score=0.55,
            retrieval_method="semantic",
        ),
    ]
    is_valid, agg_score, max_sem, methods = check_evidence_gate(chunks, min_hybrid_score=0.015)
    assert is_valid is True
    assert agg_score == pytest.approx(0.022)
    assert max_sem == 0.59


def test_evidence_gate_rejection_below_min_score():
    """Evidence Gate: Rejects if aggregated score < min_hybrid_score even if hybrid."""
    chunks = [
        SearchResult(
            chunk_text="weak hybrid match",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=1,
            score=0.010,  # Below 0.015 threshold
            raw_semantic_score=0.54,
            retrieval_method="hybrid",
        )
    ]
    is_valid, agg_score, _, _ = check_evidence_gate(chunks, min_hybrid_score=0.015)
    assert is_valid is False
    assert agg_score == 0.010


def test_evidence_gate_rejection_uncorroborated():
    """Evidence Gate: Rejects if score clears threshold but semantic similarity is too weak and not hybrid."""
    chunks = [
        SearchResult(
            chunk_text="pure keyword match without semantic corroboration",
            item_key="KEYB",
            item_id=1,
            heading_path=[],
            chunk_index=1,
            score=0.025,
            raw_semantic_score=0.51,  # Below 0.65 (single) and 0.58 (multi)
            retrieval_method="keyword",
        )
    ]
    is_valid, _, _, _ = check_evidence_gate(chunks, min_hybrid_score=0.015)
    assert is_valid is False


# ---------------------------------------------------------------------------
# 5. Edge Upsert, Symmetrization & ON CONFLICT Semantics
# ---------------------------------------------------------------------------


def test_upsert_edge_symmetrization(tmp_path: Path):
    """Verify upsert_edge persists both (A, B) and (B, A) with canonical max score."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    conn = _open_meta_db(cache_dir)

    upsert_edge(
        conn=conn,
        source_key="KEYA",
        target_key="KEYB",
        score=0.88,
        relation_type="hybrid_search",
        match_evidence=json.dumps({"test": 1}),
        symmetrize=True,
    )

    rows = conn.execute("SELECT source_key, target_key, score, relation_type FROM item_relations").fetchall()
    assert len(rows) == 2

    by_pair = {(r["source_key"], r["target_key"]): r["score"] for r in rows}
    assert by_pair[("KEYA", "KEYB")] == 0.88
    assert by_pair[("KEYB", "KEYA")] == 0.88
    conn.close()


def test_upsert_edge_on_conflict_max_score(tmp_path: Path):
    """Verify ON CONFLICT updates to higher score and refuses lower score."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    conn = _open_meta_db(cache_dir)

    # 1. First upsert with 0.75
    upsert_edge(
        conn=conn,
        source_key="KEYA",
        target_key="KEYB",
        score=0.75,
        relation_type="hybrid_search",
        symmetrize=True,
    )
    row = conn.execute(
        "SELECT score FROM item_relations WHERE source_key = 'KEYA' AND target_key = 'KEYB'"
    ).fetchone()
    assert row["score"] == 0.75

    # 2. Second upsert with higher score 0.92 -> should update to 0.92
    upsert_edge(
        conn=conn,
        source_key="KEYA",
        target_key="KEYB",
        score=0.92,
        relation_type="hybrid_search",
        symmetrize=True,
    )
    row = conn.execute(
        "SELECT score FROM item_relations WHERE source_key = 'KEYA' AND target_key = 'KEYB'"
    ).fetchone()
    assert row["score"] == 0.92

    # 3. Third upsert with lower score 0.65 -> should KEEP 0.92 due to max(item_relations.score, excluded.score)
    upsert_edge(
        conn=conn,
        source_key="KEYA",
        target_key="KEYB",
        score=0.65,
        relation_type="hybrid_search",
        symmetrize=False,
    )
    row = conn.execute(
        "SELECT score FROM item_relations WHERE source_key = 'KEYA' AND target_key = 'KEYB'"
    ).fetchone()
    assert row["score"] == 0.92
    conn.close()


def test_upsert_edge_self_edge_prevention(tmp_path: Path):
    """Verify upsert_edge ignores self-edges."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    conn = _open_meta_db(cache_dir)

    upsert_edge(
        conn=conn,
        source_key="KEYA",
        target_key="KEYA",
        score=1.0,
        relation_type="human_curated",
    )
    rows = conn.execute("SELECT * FROM item_relations").fetchall()
    assert len(rows) == 0
    conn.close()


# ---------------------------------------------------------------------------
# 6. Ground Truth Ingestion (Manual Links & Shared Tags)
# ---------------------------------------------------------------------------


def test_sync_manual_relations_and_promotion(tmp_path: Path):
    """Verify manual links from zotero.sqlite are inserted and promote hybrid edges to 1.0."""
    zotero_dir = tmp_path / "Zotero"
    zotero_dir.mkdir(parents=True)
    z_db = zotero_dir / "zotero.sqlite"

    with sqlite3.connect(str(z_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
            CREATE TABLE itemRelations (itemID INTEGER, predicateID INTEGER, object TEXT);

            INSERT INTO items (itemID, key) VALUES (1, 'PAPERA');
            INSERT INTO items (itemID, key) VALUES (2, 'PAPERB');
            INSERT INTO items (itemID, key) VALUES (3, 'PAPERC');

            -- Link PAPERA <-> PAPERB
            INSERT INTO itemRelations (itemID, predicateID, object) VALUES (1, 1, 'http://zotero.org/users/0/items/PAPERB');
            -- Link PAPERA <-> PAPERC
            INSERT INTO itemRelations (itemID, predicateID, object) VALUES (1, 1, 'http://zotero.org/users/0/items/PAPERC');
            """
        )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    meta_conn = _open_meta_db(cache_dir)

    # Seed an existing hybrid_search edge for (PAPERA, PAPERB)
    upsert_edge(
        conn=meta_conn,
        source_key="PAPERA",
        target_key="PAPERB",
        score=0.82,
        relation_type="hybrid_search",
        match_evidence=json.dumps({"raw_rrf_score": 0.05}),
        symmetrize=True,
    )

    # Sync manual relations
    count = sync_manual_relations(conn=meta_conn, zotero_dir=zotero_dir)
    assert count >= 2

    # PAPERA <-> PAPERB should be promoted to human_curated_and_hybrid_search with score 1.0
    ab_row = meta_conn.execute(
        "SELECT score, relation_type, match_evidence FROM item_relations WHERE source_key = 'PAPERA' AND target_key = 'PAPERB' AND relation_type = 'human_curated_and_hybrid_search'"
    ).fetchone()
    assert ab_row is not None
    assert ab_row["score"] == 1.0
    assert "0.05" in ab_row["match_evidence"]  # Preserved hybrid search evidence

    # PAPERA <-> PAPERC should be human_curated with score 1.0
    ac_row = meta_conn.execute(
        "SELECT score, relation_type FROM item_relations WHERE source_key = 'PAPERA' AND target_key = 'PAPERC' AND relation_type = 'human_curated'"
    ).fetchone()
    assert ac_row is not None
    assert ac_row["score"] == 1.0
    meta_conn.close()


def test_sync_shared_tags(tmp_path: Path):
    """Verify shared_tags edges are created when items share >= 2 distinctive tags."""
    zotero_dir = tmp_path / "Zotero"
    zotero_dir.mkdir(parents=True)
    z_db = zotero_dir / "zotero.sqlite"

    with sqlite3.connect(str(z_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
            CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER, type INT DEFAULT 0);
            CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER);

            INSERT INTO items (itemID, key) VALUES (1, 'DOC1');
            INSERT INTO items (itemID, key) VALUES (2, 'DOC2');
            INSERT INTO items (itemID, key) VALUES (3, 'DOC3');
            INSERT INTO items (itemID, key) VALUES (4, 'DOC4');

            INSERT INTO tags (tagID, name) VALUES (1, 'machine learning');
            INSERT INTO tags (tagID, name) VALUES (2, 'vector search');
            INSERT INTO tags (tagID, name) VALUES (3, 'quantum computing');
            INSERT INTO tags (tagID, name) VALUES (4, 'to-read');  -- Generic, should be ignored
            INSERT INTO tags (tagID, name) VALUES (5, 'automatic mesh term');

            -- DOC1: machine learning (type 0), vector search (type 0), to-read (type 0)
            INSERT INTO itemTags (itemID, tagID, type) VALUES (1, 1, 0);
            INSERT INTO itemTags (itemID, tagID, type) VALUES (1, 2, 0);
            INSERT INTO itemTags (itemID, tagID, type) VALUES (1, 4, 0);

            -- DOC2: machine learning (type 0), vector search (type 0) (shares 2 distinctive user tags with DOC1)
            INSERT INTO itemTags (itemID, tagID, type) VALUES (2, 1, 0);
            INSERT INTO itemTags (itemID, tagID, type) VALUES (2, 2, 0);

            -- DOC3: quantum computing (0 shared tags)
            INSERT INTO itemTags (itemID, tagID, type) VALUES (3, 3, 0);

            -- DOC4: shares 'automatic mesh term' with DOC1, but as automatic tag (type=1), should NOT trigger shared_tags
            INSERT INTO itemTags (itemID, tagID, type) VALUES (1, 5, 1);
            INSERT INTO itemTags (itemID, tagID, type) VALUES (4, 5, 1);
            INSERT INTO itemTags (itemID, tagID, type) VALUES (4, 1, 1);
            """
        )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    meta_conn = _open_meta_db(cache_dir)

    # Seed conversion_meta
    meta_conn.execute(
        "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, content_type, converted_at, source_hash, char_count) "
        "VALUES ('DOC1', 'test', '1.0', 'p1', 'pdf', 'now', 'h1', 100), "
        "       ('DOC2', 'test', '1.0', 'p2', 'pdf', 'now', 'h2', 100), "
        "       ('DOC3', 'test', '1.0', 'p3', 'pdf', 'now', 'h3', 100)"
    )

    created = sync_shared_tags(conn=meta_conn, zotero_dir=zotero_dir, min_shared_tags=2)
    assert created >= 1

    # DOC1 <-> DOC2 should have shared_tags edge with score 0.70
    edge = meta_conn.execute(
        "SELECT score, relation_type, match_evidence FROM item_relations WHERE source_key = 'DOC1' AND target_key = 'DOC2'"
    ).fetchone()
    assert edge is not None
    assert edge["score"] == 0.70
    assert edge["relation_type"] == "shared_tags"
    evidence = json.loads(edge["match_evidence"])
    assert "machine learning" in evidence["shared_tags"]
    assert "vector search" in evidence["shared_tags"]

    # DOC3 should have 0 edges
    doc3_edges = meta_conn.execute(
        "SELECT * FROM item_relations WHERE source_key = 'DOC3' OR target_key = 'DOC3'"
    ).fetchall()
    assert len(doc3_edges) == 0
    meta_conn.close()


# ---------------------------------------------------------------------------
# 7. End-to-End Evaluation & Sibling Exclusion
# ---------------------------------------------------------------------------


def test_evaluate_and_persist_edges_with_sibling_exclusion(tmp_path: Path):
    """Verify evaluate_and_persist_edges excludes companion attachments of same parent work."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    meta_conn = _open_meta_db(cache_dir)

    # Item setup:
    # ATT1 = Primary paper attachment (parent P1)
    # ATT2 = Supplementary appendix attachment (parent P1) -> Companion sibling of ATT1!
    # ATT3 = External paper attachment (parent P2) -> Unrelated external paper!
    meta_conn.execute(
        """
        INSERT INTO conversion_meta (
            item_key, parent_item_key, title, converter_id, converter_version,
            source_path, content_type, converted_at, source_hash, char_count, keywords
        ) VALUES
        ('ATT1', 'P1', 'Primary Research Paper', 'test', '1.0', 's1', 'application/pdf', 'now', 'h1', 1000, '["open knowledge", "trust signals"]'),
        ('ATT2', 'P1', 'Supplementary Appendix', 'test', '1.0', 's2', 'application/pdf', 'now', 'h2', 500, '["open knowledge", "datasets"]'),
        ('ATT3', 'P2', 'External Data Standards Paper', 'test', '1.0', 's3', 'application/pdf', 'now', 'h3', 1200, '["trust signals", "provenance"]')
        """
    )
    meta_conn.commit()
    meta_conn.close()

    # Create dummy bundle directories and documents
    for k in ["ATT1", "ATT2", "ATT3"]:
        bundle = cache_dir / k
        bundle.mkdir(parents=True)
        (bundle / "document.md").write_text(f"---\ntitle: Doc {k}\n---\nBody text for {k}.", encoding="utf-8")

    # Mock syncer and vector store
    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer._zotero_data_dir = tmp_path / "Zotero"

    # Search returns 2 chunks: one from sibling ATT2 and one from external ATT3
    chunk_sibling = SearchResult(
        chunk_text="sibling appendix data",
        item_key="ATT2",
        item_id=2,
        heading_path=[],
        chunk_index=1,
        score=0.040,
        raw_semantic_score=0.62,
        retrieval_method="hybrid",
    )
    chunk_external = SearchResult(
        chunk_text="external corroborated data",
        item_key="ATT3",
        item_id=3,
        heading_path=[],
        chunk_index=1,
        score=0.035,
        raw_semantic_score=0.60,
        retrieval_method="hybrid",
    )
    mock_syncer.store.search.return_value = [chunk_sibling, chunk_external]

    # Evaluate edges for ATT1
    edges = evaluate_and_persist_edges(item_key="ATT1", syncer=mock_syncer, sync_bundle_files=True)

    # Verify that sibling ATT2 was strictly excluded
    target_keys = {e["target_key"] for e in edges}
    assert "ATT2" not in target_keys, "Sibling attachment ATT2 of parent P1 was not excluded!"

    # Verify that external paper ATT3 was included
    assert "ATT3" in target_keys
    external_edge = next(e for e in edges if e["target_key"] == "ATT3")
    assert external_edge["relation_type"] == "hybrid_search"
    assert external_edge["score"] >= 0.50

    # Verify item_relations.md was materialized for ATT1 and ATT3
    att1_rel_file = cache_dir / "ATT1" / "item_relations.md"
    att3_rel_file = cache_dir / "ATT3" / "item_relations.md"
    assert att1_rel_file.exists()
    assert att3_rel_file.exists()

    att1_content = att1_rel_file.read_text(encoding="utf-8")
    assert "ATT3" in att1_content
    assert "ATT2" not in att1_content


def test_conversion_hooks_keywords_into_frontmatter_and_meta(tmp_path: Path):
    """Verify DocumentProcessor._save_to_cache extracts keywords and bakes them into document.md and DB."""
    from zocrates.rag.document_processor import DocumentProcessor
    import yaml

    cache_dir = tmp_path / "md_cache"
    source_file = tmp_path / "paper.pdf"
    source_file.write_text("dummy pdf contents", encoding="utf-8")

    text_content = """# Introduction
Lead prose discussing research artifacts.
### Cryptographic Trust Signals
We anchor immutable cryptographic hash digests to academic bundles.
### Verifiable Provenance Architecture
Provenance sidecars prevent hallucination in downstream retrieval.
"""

    with patch("zocrates.rag.document_converters.MarkItDown") as mock_mid_cls:
        mock_mid = MagicMock()
        mock_convert_result = MagicMock()
        mock_convert_result.text_content = text_content
        mock_mid.convert.return_value = mock_convert_result
        mock_mid_cls.return_value = mock_mid

        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert(
            item_key="KW_TEST_01",
            source_path=source_file,
            content_type="application/pdf",
            title="Cryptographic Trust Signals in Academic Bundles",
        )

    # 1. Verify document.md has keywords in frontmatter
    doc_path = cache_dir / "KW_TEST_01" / "document.md"
    assert doc_path.exists()
    doc_text = doc_path.read_text(encoding="utf-8")
    assert "keywords:" in doc_text

    # Parse YAML frontmatter
    parts = doc_text.split("---")
    assert len(parts) >= 3
    fm_data = yaml.safe_load(parts[1])
    assert "keywords" in fm_data
    assert isinstance(fm_data["keywords"], list)
    assert len(fm_data["keywords"]) > 0

    # 2. Verify conversion_meta.db stores keywords
    meta_db_path = cache_dir / "conversion_meta.db"
    assert meta_db_path.exists()
    with sqlite3.connect(str(meta_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT keywords FROM conversion_meta WHERE item_key = 'KW_TEST_01'"
        ).fetchone()
        assert row is not None
        assert row["keywords"] is not None
        parsed_db_kw = json.loads(row["keywords"])
        assert isinstance(parsed_db_kw, list)
        assert len(parsed_db_kw) > 0
        assert parsed_db_kw == fm_data["keywords"]

