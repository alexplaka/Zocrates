"""Unit tests for Zocrates Decoupled Core Toolkit (Phase 1B).

Covers:
- VectorStore Range-Aware line intersection queries (Option B)
- Pure Python toolkit (`search_corpus_core`, `get_document_toc_core`, `read_document_core`)
- ADK FunctionTool wrappers with ToolContext state integration
- Session state keys and evidence tracking schemas
- Prompt formatters and lifecycle callbacks
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.agents.common.prompts import (
    format_active_items,
    format_retrieved_evidence_str,
)
from zocrates.agents.common.state_keys import (
    ACTIVE_CONTEXT_ITEMS,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEARCH_REPORT,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
)
from zocrates.agents.common.toolkit import (
    get_document_toc_core,
    read_document_core,
    search_corpus_core,
)
from zocrates.agents.common.tools import (
    conclude_search,
    get_common_tools,
    get_document_toc,
    get_related_items,
    read_document,
    request_search_extension,
    search_corpus,
)
from zocrates.rag.chunker import ChunkResult
from zocrates.rag.vector_store import SearchResult, VectorStore


# ---------------------------------------------------------------------------
# 1. VectorStore Helper Methods (Option B Range-Aware Queries)
# ---------------------------------------------------------------------------


def test_vector_store_range_aware_chunk_ids(tmp_path: Path):
    """Verify get_intersecting_chunk_ids and get_all_chunk_ids_for_item on real SQLite."""
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.side_effect = lambda texts, **kw: [[0.1] * 768 for _ in texts]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    # Ingest 4 chunks with line spans: [1, 10], [11, 20], [21, 30], [31, 40]
    chunks = [
        ChunkResult("Chunk 0", ["Heading"], 0, chunk_type="frontmatter", line_start=1, line_end=10),
        ChunkResult("Chunk 1", ["Heading"], 1, chunk_type="text", line_start=11, line_end=20),
        ChunkResult("Chunk 2", ["Heading"], 2, chunk_type="text", line_start=21, line_end=30),
        ChunkResult("Chunk 3", ["Heading"], 3, chunk_type="text", line_start=31, line_end=40),
    ]
    vs.ingest_document("PAPER1", 101, chunks)

    # Intersection query for section spanning lines 15 to 25:
    # Must intersect Chunk 1 [11, 20] and Chunk 2 [21, 30].
    # Must NOT intersect Chunk 0 [1, 10] or Chunk 3 [31, 40].
    intersecting = vs.get_intersecting_chunk_ids("PAPER1", start_line=15, end_line=25)
    assert intersecting == ["PAPER1::1", "PAPER1::2"]

    # Point intersection: exactly line 10
    inter_10 = vs.get_intersecting_chunk_ids("PAPER1", start_line=10, end_line=10)
    assert inter_10 == ["PAPER1::0"]

    # Query all chunk IDs
    all_chunks = vs.get_all_chunk_ids_for_item("PAPER1")
    assert all_chunks == ["PAPER1::0", "PAPER1::1", "PAPER1::2", "PAPER1::3"]

    # Non-existent paper returns empty list
    assert vs.get_intersecting_chunk_ids("UNKNOWN", 1, 100) == []
    assert vs.get_all_chunk_ids_for_item("UNKNOWN") == []


# ---------------------------------------------------------------------------
# 2. Pure Toolkit: search_corpus_core
# ---------------------------------------------------------------------------


def test_search_corpus_core_empty_query():
    """Empty or whitespace queries must short-circuit without calling services."""
    res1 = search_corpus_core("")
    assert res1 == {"status": "success", "results": []}

    res2 = search_corpus_core("   \n\t ")
    assert res2 == {"status": "success", "results": []}


def test_search_corpus_core_prefiltering_and_clean_schema():
    """search_corpus_core passes filters down and returns lean, clean agent payloads."""
    mock_syncer = MagicMock()
    mock_store = MagicMock()
    mock_db = MagicMock()
    mock_syncer.store = mock_store
    mock_syncer.db = mock_db

    fake_result = SearchResult(
        item_id=42,
        item_key="KEY123",
        chunk_index=3,
        chunk_text="Transformers use self-attention.",
        heading_path=["Model", "Architecture"],
        score=0.032,
        raw_semantic_score=0.87,
        raw_keyword_rank=1,
        retrieval_method="hybrid",
        chunk_type="text",
        image_path=None,
        caption_source=None,
        vlm_model=None,
        chunk_id="KEY123::3",
        line_start=45,
        line_end=60,
    )
    mock_store.search.return_value = [fake_result]

    mock_item = MagicMock()
    mock_item.title = "Attention Is All You Need"
    mock_db.get_item.return_value = mock_item

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = search_corpus_core(
            query="attention mechanism",
            item_key="KEY123",
            chunk_type="text",
            limit=5,
            exclude_chunk_ids={"KEY123::0"},
        )

    assert res["status"] == "success"
    assert len(res["results"]) == 1

    r0 = res["results"][0]
    assert r0["chunk_id"] == "KEY123::3"
    assert r0["item_key"] == "KEY123"
    assert r0["title"] == "Attention Is All You Need"
    assert r0["heading"] == "Model > Architecture"
    assert r0["text"] == "Transformers use self-attention."
    assert r0["word_count"] == 3
    assert r0["cosine_similarity"] == 0.87
    assert r0["rrf_score"] == 0.032
    assert r0["line_start"] == 45
    assert r0["line_end"] == 60

    # Ensure internal database integers and provenance are stripped
    assert "item_id" not in r0
    assert "caption_source" not in r0
    assert "vlm_model" not in r0

    mock_store.search.assert_called_once_with(
        query="attention mechanism",
        final_k=5,
        item_key_filter=["KEY123"],
        chunk_type_filter=["text"],
        exclude_chunk_ids={"KEY123::0"},
    )


# ---------------------------------------------------------------------------
# 3. Pure Toolkit: get_document_toc_core
# ---------------------------------------------------------------------------


def test_get_document_toc_core_path_safety():
    """Path traversal in item_key must be rejected immediately."""
    res = get_document_toc_core("../../etc/passwd")
    assert res["status"] == "error"
    assert "Invalid item key" in res["message"]


def test_get_document_toc_core_direct_and_fallback(tmp_path: Path):
    """Verify reading existing toc.md and falling back to lazy generation."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir()

    bundle_dir = cache_dir / "DOC1"
    bundle_dir.mkdir()
    toc_md_file = bundle_dir / "toc.md"
    toc_md_file.write_text("# Test Document\n\n## Table of Contents\n- [s0] Frontmatter", encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.db.get_item_by_key.return_value = None

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = get_document_toc_core("DOC1")

    assert res["status"] == "success"
    assert res["item_key"] == "DOC1"
    assert "# Test Document" in res["toc_content"]


def test_get_document_toc_core_lazy_fallback_missing_files(tmp_path: Path):
    """Verify lazy ToC generation fallback when toc.md and toc.json are initially absent."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir()

    bundle_dir = cache_dir / "LAZY1"
    bundle_dir.mkdir()
    # Only document.md exists; toc.md and toc.json are missing!
    doc_text = "---\ntitle: Lazy Paper\n---\n# 1. Introduction\nIntroduction content."
    (bundle_dir / "document.md").write_text(doc_text, encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.processor.get_metadata.return_value = {"title": "Lazy Paper"}
    mock_syncer.db.get_item_by_key.return_value = None

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = get_document_toc_core("LAZY1")

    assert res["status"] == "success"
    assert res["item_key"] == "LAZY1"
    assert "Lazy Paper" in res["toc_content"]

    # Verify both toc.md and toc.json were generated and saved to bundle directory
    assert (bundle_dir / "toc.md").exists()
    assert (bundle_dir / "toc.json").exists()


# ---------------------------------------------------------------------------
# 4. Pure Toolkit: read_document_core
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_okf_bundle(tmp_path: Path) -> tuple[Path, str]:
    """Create a fully formed test OKF bundle with sections, figures, and tables."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "TESTITEM"
    bundle_dir.mkdir(parents=True)

    markdown_content = (
        "---\ntitle: Sample Paper\n---\n"  # lines 1-3
        "Preamble text before heading.\n"     # line 4
        "# 1. Introduction\n"                 # line 5
        "This is the introduction text.\n"    # line 6
        "<figure data-type=\"image\">\n"     # line 7
        "![Chart](images/chart.png)\n"        # line 8
        "<synthetic_caption>Bar chart of results.</synthetic_caption>\n"  # line 9
        "</figure>\n"                         # line 10
        "# 2. Results Table\n"                # line 11
        "| Metric | Score |\n"                # line 12
        "| --- | --- |\n"                     # line 13
        "| Acc | 95% |\n"                     # line 14
    )
    (bundle_dir / "document.md").write_text(markdown_content, encoding="utf-8")

    toc_json_data = {
        "version": 1,
        "total_lines": 14,
        "total_words": 35,
        "sections": [
            {"id": "s0", "heading": "(Preamble)", "start_line": 1, "end_line": 4, "word_count": 7},
            {"id": "s1", "heading": "1. Introduction", "start_line": 5, "end_line": 6, "word_count": 8},
            {"id": "s2", "heading": "2. Results Table", "start_line": 11, "end_line": 14, "word_count": 9},
        ],
        "figures": [
            {
                "id": "f1",
                "caption": "Bar chart of results",
                "path": "images/chart.png",
                "start_line": 7,
                "end_line": 10,
                "word_count": 11,
            }
        ],
        "tables": [
            {
                "id": "t1",
                "caption": "Metric Table",
                "path": "tables/table_001.csv",
                "start_line": 12,
                "end_line": 14,
                "word_count": 6,
            }
        ],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_json_data), encoding="utf-8")

    # Add CSV table file
    tables_dir = bundle_dir / "tables"
    tables_dir.mkdir()
    (tables_dir / "table_001.csv").write_text("Metric,Score\nAcc,95%\n", encoding="utf-8")

    return cache_dir, "TESTITEM"


def test_read_document_core_section_slicing(sample_okf_bundle):
    """Verify exact line slicing for sections s0 and s1 with Option B chunk interception."""
    cache_dir, item_key = sample_okf_bundle

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["TESTITEM::1"]

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res_s0 = read_document_core(item_key, asset_id="s0")
        assert res_s0["status"] == "success"
        assert len(res_s0["assets"]) == 1
        asset0 = res_s0["assets"][0]
        assert asset0["asset_id"] == "s0"
        assert "title: Sample Paper" in asset0["content"]
        assert "Preamble text" in asset0["content"]

        res_s1 = read_document_core(item_key, asset_id="s1")
        assert res_s1["status"] == "success"
        asset1 = res_s1["assets"][0]
        assert asset1["asset_id"] == "s1"
        assert asset1["heading"] == "1. Introduction"
        assert "This is the introduction text" in asset1["content"]
        assert res_s1["intersecting_chunk_ids"] == ["TESTITEM::1"]


def test_read_document_core_figure_and_table(sample_okf_bundle):
    """Verify figure image path and table structured CSV extraction."""
    cache_dir, item_key = sample_okf_bundle

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = []

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # Figure
        res_f = read_document_core(item_key, asset_id="f1")
        assert res_f["status"] == "success"
        fig_asset = res_f["assets"][0]
        assert fig_asset["asset_id"] == "f1"
        assert fig_asset["asset_type"] == "figure"
        assert fig_asset["path"] == "images/chart.png"
        assert "Bar chart of results" in fig_asset["content"]

        # Table
        res_t = read_document_core(item_key, asset_id="t1")
        assert res_t["status"] == "success"
        tab_asset = res_t["assets"][0]
        assert tab_asset["asset_id"] == "t1"
        assert tab_asset["asset_type"] == "table"
        assert tab_asset["structured_content"] == "Metric,Score\nAcc,95%\n"


def test_read_document_core_fuzzy_heading_and_multi_id(sample_okf_bundle):
    """Verify fuzzy heading resolution and multi-asset requests."""
    cache_dir, item_key = sample_okf_bundle

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["TESTITEM::2"]

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # Fuzzy match heading: "Introduction"
        res_fuzzy = read_document_core(item_key, asset_id="Introduction")
        assert res_fuzzy["status"] == "success"
        assert res_fuzzy["assets"][0]["asset_id"] == "s1"

        # Multi-ID request
        res_multi = read_document_core(item_key, asset_id=["s1", "f1"])
        assert res_multi["status"] == "success"
        assert len(res_multi["assets"]) == 2
        assert res_multi["assets"][0]["asset_id"] == "s1"
        assert res_multi["assets"][1]["asset_id"] == "f1"


def test_read_document_core_abstract_from_metadata(tmp_path: Path):
    """Verify virtual asset_id='abstract' returns toc.json metadata abstract when no section exists."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ITEM_ABS_META"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "document.md").write_text("---\ntitle: Meta Abs\n---\n# 1 Intro\nBody", encoding="utf-8")
    toc_data = {
        "title": "Meta Abs",
        "item_key": "ITEM_ABS_META",
        "abstract": "This is the paper abstract provided via Zotero metadata.",
        "abstract_source": "zotero",
        "sections": [
            {"id": "s0", "heading": "Frontmatter", "start_line": 1, "end_line": 3, "word_count": 5},
            {"id": "s1", "heading": "1 Intro", "start_line": 4, "end_line": 5, "word_count": 4},
        ],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = []

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = read_document_core("ITEM_ABS_META", asset_id="abstract")
        assert res["status"] == "success"
        assert len(res["assets"]) == 1
        asset = res["assets"][0]
        assert asset["asset_id"] == "abstract"
        assert asset["asset_type"] == "abstract"
        assert asset["heading"] == "Abstract"
        assert asset["content"] == "This is the paper abstract provided via Zotero metadata."
        assert asset["word_count"] == 9
        assert res["intersecting_chunk_ids"] == []

        # Alias "summary" works identically
        res_sum = read_document_core("ITEM_ABS_META", asset_id="summary")
        assert res_sum["status"] == "success"
        assert res_sum["assets"][0]["asset_id"] == "abstract"


def test_read_document_core_abstract_delegates_to_section(tmp_path: Path):
    """Verify virtual asset_id='abstract' delegates to an explicit section when one exists."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ITEM_ABS_SEC"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "document.md").write_text("---\ntitle: Doc\n---\n# Abstract\nIn-body abstract text.\n# 1 Intro\nIntro", encoding="utf-8")
    toc_data = {
        "title": "Doc",
        "item_key": "ITEM_ABS_SEC",
        "abstract": "Metadata abstract",
        "abstract_source": "zotero",
        "sections": [
            {"id": "s0", "heading": "Frontmatter", "start_line": 1, "end_line": 3, "word_count": 5},
            {"id": "s1", "heading": "Abstract", "start_line": 4, "end_line": 5, "word_count": 4},
            {"id": "s2", "heading": "1 Intro", "start_line": 6, "end_line": 7, "word_count": 2},
        ],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["ITEM_ABS_SEC::1"]

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = read_document_core("ITEM_ABS_SEC", asset_id="abstract")
        assert res["status"] == "success"
        assert len(res["assets"]) == 1
        asset = res["assets"][0]
        assert asset["asset_id"] == "s1"
        assert asset["asset_type"] == "section"
        assert "In-body abstract text." in asset["content"]
        assert res["intersecting_chunk_ids"] == ["ITEM_ABS_SEC::1"]


def test_read_document_core_abstract_missing_raises(tmp_path: Path):
    """Verify error response when document has no abstract in sections or metadata."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ITEM_NO_ABS"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "document.md").write_text("---\ntitle: Doc\n---\n# 1 Intro\nIntro", encoding="utf-8")
    toc_data = {
        "title": "Doc",
        "item_key": "ITEM_NO_ABS",
        "abstract": "No abstract or summary available.",
        "abstract_source": "extractive",
        "sections": [
            {"id": "s0", "heading": "Frontmatter", "start_line": 1, "end_line": 3, "word_count": 5},
            {"id": "s1", "heading": "1 Intro", "start_line": 4, "end_line": 5, "word_count": 2},
        ],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = read_document_core("ITEM_NO_ABS", asset_id="abstract")
        assert res["status"] == "error"
        assert "No abstract or summary available" in res["message"]


def test_read_document_core_error_handling_and_path_safety(tmp_path: Path):
    """Verify error responses for path traversal, missing bundles, and missing document.md."""
    # 1. Path traversal rejected
    res_trav = read_document_core("../../etc/passwd")
    assert res_trav["status"] == "error"
    assert "Invalid item key" in res_trav["message"]

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.db.get_item_by_key.return_value = None

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # 2. Bundle not found
        res_missing = read_document_core("NONEXISTENT")
        assert res_missing["status"] == "error"
        assert "not found" in res_missing["message"]

        # 3. Bundle dir exists but document.md missing
        (cache_dir / "EMPTYBUNDLE").mkdir()
        res_no_doc = read_document_core("EMPTYBUNDLE")
        assert res_no_doc["status"] == "error"
        assert "not found" in res_no_doc["message"]


def test_read_document_core_full_document_pagination(tmp_path: Path):
    """Verify full document (asset_id=None) pagination and line-span chunk calculation."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "LONGDOC"
    bundle_dir.mkdir(parents=True)

    # Generate a document with 7 lines, each containing 1,000 words (total 7,000 words)
    lines = [f"word{'x'*4} " * 1000 + "\n" for _ in range(7)]
    full_text = "".join(lines)
    (bundle_dir / "document.md").write_text(full_text, encoding="utf-8")

    # Mock toc.json
    toc_data = {
        "version": 1,
        "total_lines": 7,
        "total_words": 7000,
        "sections": [{"id": "s0", "heading": "Full", "start_line": 1, "end_line": 7}],
        "figures": [],
        "tables": [],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir

    def mock_get_intersecting(key, start, end):
        return [f"{key}::{start}-{end}"]

    mock_syncer.store.get_intersecting_chunk_ids.side_effect = mock_get_intersecting

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # Page 1 (Lines 1-3 = 3000 words)
        res_p1 = read_document_core("LONGDOC", asset_id=None, page=1)
        assert res_p1["status"] == "success"
        asset1 = res_p1["assets"][0]
        assert asset1["asset_id"] == "full"
        assert asset1["page"] == 1
        assert asset1["total_pages"] == 3
        assert asset1["has_more"] is True
        assert asset1["word_count"] == 3000
        # Intersecting chunk IDs queried only for page 1 line span [1, 3]!
        assert res_p1["intersecting_chunk_ids"] == ["LONGDOC::1-3"]

        # Page 2 (Lines 4-6 = 3000 words)
        res_p2 = read_document_core("LONGDOC", asset_id=None, page=2)
        asset2 = res_p2["assets"][0]
        assert asset2["page"] == 2
        assert asset2["has_more"] is True
        assert res_p2["intersecting_chunk_ids"] == ["LONGDOC::4-6"]

        # Page 3 (Line 7 = 1000 words)
        res_p3 = read_document_core("LONGDOC", asset_id=None, page=3)
        asset3 = res_p3["assets"][0]
        assert asset3["page"] == 3
        assert asset3["has_more"] is False
        assert asset3["word_count"] == 1000
        assert res_p3["intersecting_chunk_ids"] == ["LONGDOC::7-7"]


def test_read_document_core_multi_asset_word_cap(tmp_path: Path):
    """Verify multi-asset word cap truncation when accumulated words exceed MULTI_ASSET_WORD_CAP (6000 words)."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "CAPDOC"
    bundle_dir.mkdir(parents=True)

    sec1_text = "# Section 1\n" + ("sec1word " * 2500) + "\n"
    sec2_text = "# Section 2\n" + ("sec2word " * 2500) + "\n"
    sec3_text = "# Section 3\n" + ("sec3word " * 2500) + "\n"
    (bundle_dir / "document.md").write_text(sec1_text + sec2_text + sec3_text, encoding="utf-8")

    sec1_lines = len(sec1_text.splitlines())
    sec2_lines = len(sec2_text.splitlines())
    sec3_lines = len(sec3_text.splitlines())

    toc_data = {
        "version": 1,
        "sections": [
            {"id": "s1", "heading": "Section 1", "start_line": 1, "end_line": sec1_lines, "word_count": 2501},
            {"id": "s2", "heading": "Section 2", "start_line": sec1_lines + 1, "end_line": sec1_lines + sec2_lines, "word_count": 2501},
            {"id": "s3", "heading": "Section 3", "start_line": sec1_lines + sec2_lines + 1, "end_line": sec1_lines + sec2_lines + sec3_lines, "word_count": 2501},
        ],
        "figures": [],
        "tables": [],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = []

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = read_document_core("CAPDOC", asset_id=["s1", "s2", "s3"])
        assert res["status"] == "success"
        assets = res["assets"]
        assert len(assets) == 3
        # First two assets fit under cap (2502 words each = 5004 words)
        assert assets[0]["asset_id"] == "s1"
        assert assets[0]["has_more"] is False
        assert assets[1]["asset_id"] == "s2"
        assert assets[1]["has_more"] is False
        # Third asset is truncated to reach exactly MULTI_ASSET_WORD_CAP (6000 words)
        assert assets[2]["asset_id"] == "s3"
        assert assets[2]["has_more"] is True
        total_w = sum(a["word_count"] for a in assets)
        assert total_w == 6000


# ---------------------------------------------------------------------------
# 5. ADK FunctionTools & State Integration (`tools.py`)
# ---------------------------------------------------------------------------


class MockToolContext:
    """Mock ADK ToolContext providing dictionary-like session/invocation state."""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}
        self.actions = MagicMock()


def test_search_corpus_tool_state_integration():
    """search_corpus updates seen_chunk_ids and appends to retrieved_evidence in ToolContext."""
    mock_ctx = MockToolContext(
        {
            ACTIVE_CONTEXT_ITEMS: [{"key": "SEL1", "title": "Paper 1"}],
            TEMP_SEEN_CHUNK_IDS: ["SEL1::0"],
            TEMP_RETRIEVED_EVIDENCE: [],
        }
    )

    fake_core_result = {
        "status": "success",
        "results": [
            {
                "chunk_id": "SEL1::1",
                "item_key": "SEL1",
                "title": "Paper 1",
                "chunk_type": "text",
                "heading": "Intro",
                "text": "Some text",
                "word_count": 2,
                "cosine_similarity": 0.85,
                "rrf_score": 0.03,
                "image_path": None,
            }
        ],
    }

    with patch("zocrates.agents.common.tools.search_corpus_core", return_value=fake_core_result) as mock_core:
        res = search_corpus("quantum", tool_context=mock_ctx)

    # Assert search_corpus_core was called with active context item key (default="active")
    mock_core.assert_called_once_with(
        query="quantum",
        item_key=["SEL1"],
        chunk_type=None,
        limit=10,
        exclude_chunk_ids=["SEL1::0"],
    )

    # Assert calling with item_key=None explicitly searches the entire library
    with patch("zocrates.agents.common.tools.search_corpus_core", return_value=fake_core_result) as mock_core_library:
        res_library = search_corpus("quantum", item_key=None, tool_context=mock_ctx)
    call_kwargs = mock_core_library.call_args[1]
    assert call_kwargs["query"] == "quantum"
    assert call_kwargs["item_key"] is None
    assert call_kwargs["chunk_type"] is None
    assert call_kwargs["limit"] == 10
    assert set(call_kwargs["exclude_chunk_ids"]) == {"SEL1::0", "SEL1::1"}

    # State checks
    seen = mock_ctx.state[TEMP_SEEN_CHUNK_IDS]
    assert "SEL1::0" in seen
    assert "SEL1::1" in seen

    ev = mock_ctx.state[TEMP_RETRIEVED_EVIDENCE]
    assert len(ev) == 2
    assert ev[0]["chunk_id"] == "SEL1::1"
    assert ev[0]["retrieval_method"] == "hybrid"

    assert "retrieval_status" in res
    assert "1 new chunks retrieved" in res["retrieval_status"]


def test_search_corpus_chunk_type_normalization():
    """Verify search_corpus_core normalizes 'none', 'null', 'all', '*', and '' to chunk_type_filter=None."""
    mock_syncer = MagicMock()
    mock_syncer.store.search.return_value = []
    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        for raw_ct in ("none", "NONE", "null", "all", "*", "", ["none", "null"]):
            search_corpus_core("query", chunk_type=raw_ct)
            mock_syncer.store.search.assert_called_with(
                query="query",
                final_k=10,
                item_key_filter=None,
                chunk_type_filter=None,
                exclude_chunk_ids=None,
            )
        # Explicit valid type should be passed as lowercase list
        search_corpus_core("query", chunk_type="TEXT")
        mock_syncer.store.search.assert_called_with(
            query="query",
            final_k=10,
            item_key_filter=None,
            chunk_type_filter=["text"],
            exclude_chunk_ids=None,
        )


def test_read_document_tool_option_b_and_asset_dedup():
    """read_document performs intra-tool dedup and injects Option B intersecting chunks into seen_chunk_ids."""
    mock_ctx = MockToolContext(
        {
            TEMP_SEEN_ASSETS: [],
            TEMP_SEEN_CHUNK_IDS: [],
            TEMP_RETRIEVED_EVIDENCE: [],
        }
    )

    fake_read_result = {
        "status": "success",
        "item_key": "ITEMA",
        "assets": [
            {
                "asset_id": "s3",
                "asset_type": "section",
                "heading": "Methods",
                "content": "Detailed methodology description.",
                "word_count": 3,
                "path": None,
                "structured_content": None,
                "page": 1,
                "total_pages": 1,
                "has_more": False,
            }
        ],
        "intersecting_chunk_ids": ["ITEMA::4", "ITEMA::5"],
    }

    with patch("zocrates.agents.common.tools.read_document_core", return_value=fake_read_result):
        res = read_document("ITEMA", asset_id="s3", tool_context=mock_ctx)

    assert res["status"] == "success"

    # Option B Range-Aware Pre-Exclusion check:
    # Vector chunk IDs ITEMA::4 and ITEMA::5 must be in temp:seen_chunk_ids!
    seen_chunks = mock_ctx.state[TEMP_SEEN_CHUNK_IDS]
    assert "ITEMA::4" in seen_chunks
    assert "ITEMA::5" in seen_chunks

    # Asset dedup check:
    assert "ITEMA:s3" in mock_ctx.state[TEMP_SEEN_ASSETS]

    # Evidence chunk format check:
    ev = mock_ctx.state[TEMP_RETRIEVED_EVIDENCE]
    assert len(ev) == 1
    assert ev[0]["chunk_id"] == "ITEMA:s3"
    assert ev[0]["retrieval_method"] == "direct_read"

    # Second call for the same asset should be rejected by intra-tool dedup
    res_duplicate = read_document("ITEMA", asset_id="s3", tool_context=mock_ctx)
    assert res_duplicate["status"] == "success"
    assert res_duplicate["assets"] == []
    assert "already retrieved" in res_duplicate["notice"]


def test_read_document_tool_full_doc_dedup_and_fuzzy_resolution():
    """Verify full-document dedup (asset_id=None) and fuzzy heading canonical ID tracking."""
    mock_ctx = MockToolContext(
        {
            TEMP_SEEN_ASSETS: [],
            TEMP_SEEN_CHUNK_IDS: [],
            TEMP_RETRIEVED_EVIDENCE: [],
        }
    )

    fake_full_res = {
        "status": "success",
        "item_key": "DOCA",
        "assets": [
            {
                "asset_id": "full",
                "asset_type": "document",
                "heading": "Full Document",
                "content": "Full content.",
                "word_count": 2,
                "path": None,
                "structured_content": None,
                "page": 1,
                "total_pages": 1,
                "has_more": False,
            }
        ],
        "intersecting_chunk_ids": ["DOCA::0"],
    }

    # 1. Full document read
    with patch("zocrates.agents.common.tools.read_document_core", return_value=fake_full_res):
        res1 = read_document("DOCA", asset_id=None, tool_context=mock_ctx)
        assert res1["status"] == "success"
        assert len(res1["assets"]) == 1

        # Second full document read must be rejected by intra-tool dedup
        res2 = read_document("DOCA", asset_id=None, tool_context=mock_ctx)
        assert res2["status"] == "success"
        assert res2["assets"] == []
        assert "already retrieved" in res2["notice"]

    # 2. Fuzzy heading resolution records canonical ID
    fake_fuzzy_res = {
        "status": "success",
        "item_key": "DOCA",
        "assets": [
            {
                "asset_id": "s1",
                "asset_type": "section",
                "heading": "1. Introduction",
                "content": "Intro text.",
                "word_count": 2,
                "path": None,
                "structured_content": None,
                "page": 1,
                "total_pages": 1,
                "has_more": False,
            }
        ],
        "intersecting_chunk_ids": ["DOCA::1"],
    }

    with patch("zocrates.agents.common.tools.read_document_core", return_value=fake_fuzzy_res):
        res_f1 = read_document("DOCA", asset_id="Introduction", tool_context=mock_ctx)
        assert res_f1["status"] == "success"
        assert len(res_f1["assets"]) == 1

        # Both raw query "DOCA:Introduction" and canonical "DOCA:s1" must be recorded
        assert "DOCA:Introduction" in mock_ctx.state[TEMP_SEEN_ASSETS]
        assert "DOCA:s1" in mock_ctx.state[TEMP_SEEN_ASSETS]

        # Reset tool count for the next step
        mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 0

        # Querying by canonical ID "s1" is now also deduplicated
        res_canonical = read_document("DOCA", asset_id="s1", tool_context=mock_ctx)
        assert res_canonical["status"] == "success"
        assert res_canonical["assets"] == []
        assert "already retrieved" in res_canonical["notice"]


def test_get_common_tools_registry():
    """Verify that get_common_tools exports the core retrieval and termination tools."""
    tools = get_common_tools()
    assert len(tools) == 6
    assert search_corpus in tools
    assert get_document_toc in tools
    assert read_document in tools
    assert get_related_items in tools
    assert conclude_search in tools
    assert request_search_extension in tools


def test_conclude_search_and_budget_enforcement():
    """Verify termination tool and 10-call retrieval tool budget."""
    mock_ctx = MockToolContext(
        {
            TEMP_SEEN_ASSETS: [],
            TEMP_SEEN_CHUNK_IDS: [],
            TEMP_RETRIEVED_EVIDENCE: [],
            TEMP_RETRIEVAL_TOOL_COUNT: 0,
            TEMP_SEARCH_REPORT: None,
        }
    )

    # Call conclude_search directly
    rep_res = conclude_search(
        summary="Found 3 key sections.",
        notes="methodology and data",
        gaps=["evaluation"],
        tool_context=mock_ctx,
    )
    assert rep_res["status"] == "search_concluded"
    assert mock_ctx.actions.skip_summarization is True
    report = mock_ctx.state[TEMP_SEARCH_REPORT]
    assert report["summary"] == "Found 3 key sections."
    assert report["gaps"] == ["evaluation"]

    # Now verify 10 retrieval calls are allowed and 11th is blocked
    mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 0
    mock_ctx.state[TEMP_SEARCH_REPORT] = None

    with patch("zocrates.agents.common.tools.get_document_toc_core", return_value={"status": "success", "toc_content": "# ToC"}):
        # Calls 1 through 4: no notice
        for i in range(1, 5):
            res = get_document_toc(f"DOC{i}", tool_context=mock_ctx)
            assert res["status"] == "success"
            assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == i
            assert "budget_notice" not in res

        # Calls 5 through 8: midpoint notice
        for i in range(5, 9):
            res = get_document_toc(f"DOC{i}", tool_context=mock_ctx)
            assert res["status"] == "success"
            assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == i
            assert "budget_notice" in res
            assert f"{i}/10 retrieval tool calls used" in res["budget_notice"]
            assert "Continue your search, being mindful" in res["budget_notice"]

        # 9th call: penultimate soft warning notice
        res9 = get_document_toc("DOC9", tool_context=mock_ctx)
        assert res9["status"] == "success"
        assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 9
        assert "budget_notice" in res9
        assert "9/10" in res9["budget_notice"]

        # 10th call: budget exhausted notice and preliminary report auto-generated
        res10 = get_document_toc("DOC10", tool_context=mock_ctx)
        assert res10["status"] == "success"
        assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 10
        assert "budget_notice" in res10
        assert "budget exhausted" in res10["budget_notice"]
        assert mock_ctx.state[TEMP_SEARCH_REPORT] is not None

        # 11th call: must be blocked
        res11 = get_document_toc("DOC11", tool_context=mock_ctx)
        assert res11["status"] == "budget_exhausted"
        assert "budget reached" in res11["error"]

    # Verify word ceiling enforcement
    mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 0
    mock_ctx.state[TEMP_RETRIEVED_EVIDENCE] = [{"chunk_id": "c1", "word_count": 8000, "text": "word " * 8000}]
    word_res = get_document_toc("DOC1", tool_context=mock_ctx)
    assert word_res["status"] == "budget_exhausted"
    assert "Word ceiling reached" in word_res["error"]

    # Verify empty-query search_corpus does not burn budget
    mock_ctx.state[TEMP_RETRIEVED_EVIDENCE] = []
    mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 0
    empty_res = search_corpus("", tool_context=mock_ctx)
    assert empty_res["status"] == "error"
    assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 0

    blank_res = search_corpus("   ", tool_context=mock_ctx)
    assert blank_res["status"] == "error"
    assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 0

    # Verify read_document deduplication path on 10th call still attaches budget notice
    mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 9
    mock_ctx.state[TEMP_SEEN_ASSETS] = ["DOC1:full"]
    mock_ctx.state[TEMP_SEARCH_REPORT] = None

    dedup_res = read_document("DOC1", asset_id=None, tool_context=mock_ctx)
    assert dedup_res["status"] == "success"
    assert dedup_res["assets"] == []
    assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 10
    assert "budget_notice" in dedup_res
    assert mock_ctx.state[TEMP_SEARCH_REPORT] is not None


# ---------------------------------------------------------------------------
# 6. Prompts and Formatters
# ---------------------------------------------------------------------------


def test_format_active_items():
    """format_active_items formats Zotero active context chips."""
    assert "No active paper" in format_active_items([])
    assert "No active paper" in format_active_items(None)

    items = [
        {"key": "K1", "title": "Paper One", "year": "2023"},
        {"key": "K2", "title": "Paper Two", "year": "2024"},
    ]
    formatted = format_active_items(items)
    assert '[Key: K1] Title: "Paper One" (2023)' in formatted
    assert '[Key: K2] Title: "Paper Two" (2024)' in formatted

    # Test include_keys=False hides internal keys
    clean_formatted = format_active_items(items, include_keys=False)
    assert '[Key: K1]' not in clean_formatted
    assert '[Key: K2]' not in clean_formatted
    assert 'Title: "Paper One" (2023)' in clean_formatted
    assert 'Title: "Paper Two" (2024)' in clean_formatted


def test_format_active_items_flat_single_attachment():
    """Single-attachment items with no notes render the same flat format as before."""
    items = [
        {
            "item_key": "PARENT1",
            "title": "Attention Is All You Need",
            "year": "2017",
            "children": [
                {
                    "item_key": "ATT001",
                    "title": "vaswani2017.pdf",
                    "content_type": "application/pdf",
                    "is_note": False,
                }
            ],
        }
    ]
    result = format_active_items(items)
    # Must be byte-identical flat format when exactly 1 attachment, 0 notes
    assert result == '- [Key: PARENT1] Title: "Attention Is All You Need" (2017)'

    result_nokeys = format_active_items(items, include_keys=False)
    assert result_nokeys == '- Title: "Attention Is All You Need" (2017)'


def test_format_active_items_hierarchical_multi_attachment():
    """Multiple attachments trigger hierarchical output with Attachment Key lines."""
    items = [
        {
            "item_key": "PARENT1",
            "title": "Multi-Attachment Paper",
            "year": "2022",
            "children": [
                {
                    "item_key": "ATT001",
                    "title": "Main Article (paper.pdf)",
                    "content_type": "application/pdf",
                    "is_note": False,
                },
                {
                    "item_key": "ATT002",
                    "title": "Supplementary (supp.pdf)",
                    "content_type": "application/pdf",
                    "is_note": False,
                },
            ],
        }
    ]
    result = format_active_items(items)
    assert '[Key: PARENT1] Title: "Multi-Attachment Paper" (2022)' in result
    assert '[Attachment Key: ATT001] "Main Article (paper.pdf)" [application/pdf]' in result
    assert '[Attachment Key: ATT002] "Supplementary (supp.pdf)" [application/pdf]' in result

    result_nokeys = format_active_items(items, include_keys=False)
    assert '[Attachment Key:' not in result_nokeys
    assert '[Key:' not in result_nokeys
    assert 'Attachment: "Main Article (paper.pdf)" [application/pdf]' in result_nokeys
    assert 'Attachment: "Supplementary (supp.pdf)" [application/pdf]' in result_nokeys


def test_format_active_items_inline_note():
    """Notes with fewer than 50 words are inlined as blockquotes."""
    items = [
        {
            "item_key": "PARENT1",
            "title": "A Paper",
            "year": "2023",
            "children": [
                {
                    "item_key": "ATT001",
                    "title": "paper.pdf",
                    "content_type": "application/pdf",
                    "is_note": False,
                },
                {
                    "item_key": "NOTE001",
                    "title": "Quick Remark",
                    "content_type": "text/note",
                    "is_note": True,
                    "word_count": 10,
                    "inline_text": "Key takeaway: warmup is critical.",
                },
            ],
        }
    ]
    result = format_active_items(items)
    assert '[Note Key: NOTE001] "Quick Remark" (10 words) [text/note]:' in result
    assert '    > Key takeaway: warmup is critical.' in result

    result_nokeys = format_active_items(items, include_keys=False)
    assert '[Note Key:' not in result_nokeys
    assert 'Note: "Quick Remark":' in result_nokeys
    assert '    > Key takeaway: warmup is critical.' in result_nokeys


def test_format_active_items_reference_only_note():
    """Notes with 50+ words are referenced by key and word count, not inlined."""
    items = [
        {
            "item_key": "PARENT1",
            "title": "A Paper",
            "year": "2023",
            "children": [
                {
                    "item_key": "ATT001",
                    "title": "paper.pdf",
                    "content_type": "application/pdf",
                    "is_note": False,
                },
                {
                    "item_key": "NOTE002",
                    "title": "Detailed Literature Review",
                    "content_type": "text/note",
                    "is_note": True,
                    "word_count": 320,
                    "inline_text": None,
                },
            ],
        }
    ]
    result = format_active_items(items)
    assert '[Note Key: NOTE002] "Detailed Literature Review" (~320 words) [text/note]' in result
    # No blockquote lines
    assert '    >' not in result

    result_nokeys = format_active_items(items, include_keys=False)
    assert '[Note Key:' not in result_nokeys
    assert 'Note: "Detailed Literature Review" [text/note]' in result_nokeys


def test_format_active_items_note_only_triggers_hierarchy():
    """A single attachment + a note triggers hierarchical output (has_notes=True)."""
    items = [
        {
            "item_key": "PARENT1",
            "title": "Paper With Note",
            "year": "2021",
            "children": [
                {
                    "item_key": "ATT001",
                    "title": "paper.pdf",
                    "content_type": "application/pdf",
                    "is_note": False,
                },
                {
                    "item_key": "NNOTE01",
                    "title": "My Notes",
                    "content_type": "text/note",
                    "is_note": True,
                    "word_count": 80,
                    "inline_text": None,
                },
            ],
        }
    ]
    result = format_active_items(items)
    # Hierarchy triggered even with only 1 attachment because a note is present
    assert '[Key: PARENT1]' in result
    assert '[Attachment Key: ATT001]' in result
    assert '[Note Key: NNOTE01]' in result


def test_format_retrieved_evidence_str():
    """format_retrieved_evidence_str formats evidence chunks into clean XML for synthesizer injection."""
    empty_res = format_retrieved_evidence_str([])
    assert "<retrieved_evidence>" in empty_res
    assert "</retrieved_evidence>" in empty_res
    assert "No evidence" in empty_res

    chunks = [
        {
            "chunk_id": "ITEM::2",
            "item_key": "ITEM",
            "title": "A Great Paper",
            "chunk_type": "text",
            "heading": 'Results & "Discussion"',
            "retrieval_method": "hybrid",
            "cosine_similarity": 0.891,
            "rrf_score": 0.0315,
            "rerank_score": 0.952,
            "text": "Empirical accuracy improved by 14%.",
            "word_count": 5,
        },
        {
            "chunk_id": "ITEM::3",
            "item_key": "ITEM",
            "title": "A Great Paper",
            "chunk_type": "table",
            "structured_content": "model,score\nClaude,95",
        },
        {
            "chunk_id": "ITEM::4",
            "item_key": "ITEM",
            "title": "A Great Paper",
            "chunk_type": "figure",
            "image_path": "images/chart.png",
            "text": "Architecture overview diagram.",
        },
        {
            "chunk_id": "SECRET::5",
            "item_key": "SECRET_KEY_123",
            "title": "",  # Empty title must fall back to "Untitled Document", NEVER leak SECRET_KEY_123
            "chunk_type": "text",
            "text": "Secret content.",
        },
        {
            "chunk_id": "ESCAPED::6",
            "item_key": "ESC",
            "title": 'Paper & "Quotation" <Tag>',
            "heading": 'Section & "Subsection" <Sub>',
            "text": None,  # Robust against None text -> empty body self-closing
        },
    ]
    formatted = format_retrieved_evidence_str(chunks)

    # XML tags structure
    assert "<retrieved_evidence>" in formatted
    assert "</retrieved_evidence>" in formatted
    assert '<chunk index="1" title="A Great Paper" section="Results &amp; &quot;Discussion&quot;" type="text">' in formatted
    assert '<chunk index="2" title="A Great Paper" type="table">' in formatted
    assert '<chunk index="3" title="A Great Paper" type="figure">' in formatted
    assert '<chunk index="4" title="Untitled Document" type="text">' in formatted
    assert '<chunk index="5" title="Paper &amp; &quot;Quotation&quot; &lt;Tag&gt;" section="Section &amp; &quot;Subsection&quot; &lt;Sub&gt;" type="text" />' in formatted

    # Suppression of internal chunk IDs and telemetry
    assert "ITEM::2" not in formatted
    assert "ITEM::3" not in formatted
    assert "ITEM::4" not in formatted
    assert "SECRET::5" not in formatted
    assert "SECRET_KEY_123" not in formatted
    assert "Rerank" not in formatted
    assert "cosine_similarity" not in formatted
    assert "hybrid" not in formatted

    # Content retention
    assert "Empirical accuracy improved by 14%." in formatted
    assert "<table_csv>\nmodel,score\nClaude,95\n</table_csv>" in formatted
    # Figure chunks with item_key + image_path now emit a displayable
    # zocrates-img:// URI (Response Image Display plan, P1): the chunk above
    # carries item_key="ITEM" and image_path="images/chart.png".
    assert '<figure_image path="zocrates-img://ITEM/images/chart.png" />' in formatted
    assert "Architecture overview diagram." in formatted
    assert "Secret content." in formatted


# ---------------------------------------------------------------------------
# 7. Lifecycle Callbacks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. Asset Normalization & Robust Deduplication
# ---------------------------------------------------------------------------


def test_normalize_asset_ids():
    """Verify normalize_asset_ids handles all LLM input formats robustly."""
    from zocrates.agents.common.toolkit import normalize_asset_ids

    assert normalize_asset_ids("[s3, s4, s5]") == ["s3", "s4", "s5"]
    assert normalize_asset_ids('["s1", "s2"]') == ["s1", "s2"]
    assert normalize_asset_ids('"s1", "s2"') == ["s1", "s2"]
    assert normalize_asset_ids("'s1', 's2'") == ["s1", "s2"]
    assert normalize_asset_ids("[s1]") == ["s1"]
    assert normalize_asset_ids("s1, s2,") == ["s1", "s2"]
    assert normalize_asset_ids("s1,, s2") == ["s1", "s2"]
    assert normalize_asset_ids(["s1", "s2"]) == ["s1", "s2"]
    assert normalize_asset_ids("s3") == ["s3"]
    assert normalize_asset_ids("  s4  ") == ["s4"]
    assert normalize_asset_ids(None) is None
    assert normalize_asset_ids("") is None
    assert normalize_asset_ids("   ") is None


def test_read_document_bracketed_list_and_unresolved_reporting(sample_okf_bundle):
    """read_document_core correctly extracts all items from bracketed strings and reports unresolved IDs."""
    cache_dir, item_key = sample_okf_bundle

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["TESTITEM::2"]

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # 1. Bracketed string with all valid IDs
        res = read_document_core(item_key, "[s1, s2]")
        assert res["status"] == "success"
        returned_ids = [a["asset_id"] for a in res["assets"]]
        assert returned_ids == ["s1", "s2"]
        assert "unresolved_asset_ids" not in res

        # 2. Bracketed string with one invalid ID
        res_partial = read_document_core(item_key, "[s1, bogus_id, s2]")
        assert res_partial["status"] == "success"
        partial_returned_ids = [a["asset_id"] for a in res_partial["assets"]]
        assert partial_returned_ids == ["s1", "s2"]
        assert res_partial["unresolved_asset_ids"] == ["bogus_id"]
        assert "Warning: Asset IDs ['bogus_id'] could not be resolved" in res_partial["notice"]


def test_read_document_canonical_deduplication(sample_okf_bundle):
    """read_document uses canonical {item_key}:{aid} keys for deduplication, never raw strings."""
    cache_dir, item_key = sample_okf_bundle

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["TESTITEM::2"]

    mock_tc = MagicMock()
    mock_tc.state = {}

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # 1. Call with bracketed list
        res1 = read_document(item_key, "[s1, s2]", tool_context=mock_tc)
        assert res1["status"] == "success"
        assert len(res1["assets"]) == 2

        # Check seen_assets contains canonical keys only
        seen = set(mock_tc.state["temp:seen_assets"])
        assert f"{item_key}:s1" in seen
        assert f"{item_key}:s2" in seen
        assert f"{item_key}:[s1, s2]" not in seen

        # 2. Call again with bracketed list including an already seen ID and a new ID
        res2 = read_document(item_key, "[s1, f1]", tool_context=mock_tc)
        assert res2["status"] == "success"
        assert len(res2["assets"]) == 1
        assert res2["assets"][0]["asset_id"] == "f1"
        assert f"{item_key}:f1" in mock_tc.state["temp:seen_assets"]

        # 3. Call with all seen IDs
        res3 = read_document(item_key, "s1, f1", tool_context=mock_tc)
        assert res3["status"] == "success"
        assert len(res3["assets"]) == 0
        assert "already retrieved in this session" in res3["notice"]


def test_read_document_propagates_title_to_synthetic_evidence():
    """read_document assigns the resolved document title to synthetic evidence chunks."""
    mock_tc = MagicMock()
    mock_tc.state = {
        TEMP_SEEN_ASSETS: [],
        TEMP_SEEN_CHUNK_IDS: [],
        TEMP_RETRIEVED_EVIDENCE: [],
        ACTIVE_CONTEXT_ITEMS: [{"key": "MYDOC", "title": "Understanding Neural Networks"}],
    }

    fake_core_res = {
        "status": "success",
        "item_key": "MYDOC",
        "title": "Understanding Neural Networks",
        "assets": [
            {
                "asset_id": "s1",
                "asset_type": "section",
                "heading": "Architecture",
                "content": "Layers and weights.",
                "word_count": 3,
                "path": None,
                "structured_content": None,
                "page": 1,
                "total_pages": 1,
                "has_more": False,
            }
        ],
        "intersecting_chunk_ids": [],
    }

    with patch("zocrates.agents.common.tools.read_document_core", return_value=fake_core_res):
        res = read_document("MYDOC", asset_id="s1", tool_context=mock_tc)
        assert res["status"] == "success"

        evidence = mock_tc.state[TEMP_RETRIEVED_EVIDENCE]
        assert len(evidence) == 1
        assert evidence[0]["title"] == "Understanding Neural Networks"
        assert evidence[0]["heading"] == "Architecture"


# ---------------------------------------------------------------------------
# 9. materialize_note_bundle
# ---------------------------------------------------------------------------


def test_materialize_note_bundle_creates_bundle(tmp_path: Path):
    """Verify materialize_note_bundle creates document.md, toc files, and conversion_meta entry."""
    from zocrates.rag.document_processor import materialize_note_bundle
    from zocrates.rag.zotero_db import ZoteroNote

    note = ZoteroNote(
        note_id=1,
        note_key="NOTE001A",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="My Reading Notes",
        note_html="<h2>Summary</h2><p>The paper introduces a novel attention mechanism.</p>",
    )

    # Build a minimal mock syncer with processor
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir()

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.get_metadata.return_value = None  # No existing metadata

    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor

    note_dir, resolved_key = materialize_note_bundle(note, mock_syncer, cache_dir)

    assert resolved_key == "NOTE001A"
    assert note_dir == cache_dir / "NOTE001A"
    assert (note_dir / "document.md").exists()

    doc_text = (note_dir / "document.md").read_text(encoding="utf-8")
    # Frontmatter should be present
    assert 'title: "My Reading Notes"' in doc_text
    assert 'item_key: "NOTE001A"' in doc_text
    assert 'parent_item_key: "PARENT10"' in doc_text
    assert "type: note" in doc_text
    assert 'content_type: "text/note"' in doc_text
    assert "source:" in doc_text
    assert 'path: "zotero://note/NOTE001A"' in doc_text
    # Heading section
    assert "# My Reading Notes" in doc_text
    # Converted content
    assert "Summary" in doc_text
    assert "attention mechanism" in doc_text


def test_materialize_note_bundle_no_llm_short_note(tmp_path: Path):
    """Notes < 100 words produce abstract=None (no LLM call, no extractive summary)."""
    from zocrates.rag.document_processor import materialize_note_bundle, _open_meta_db
    from zocrates.rag.zotero_db import ZoteroNote

    short_html = "<p>Short note.</p>"  # 2 words
    note = ZoteroNote(
        note_id=2,
        note_key="SHORTNOTE",
        parent_item_id=None,
        parent_item_key=None,
        title="Short",
        note_html=short_html,
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.get_metadata.return_value = None
    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor

    note_dir, _ = materialize_note_bundle(note, mock_syncer, cache_dir)

    # Check conversion_meta.db was written with no abstract
    with _open_meta_db(cache_dir) as conn:
        row = conn.execute(
            "SELECT abstract, abstract_source FROM conversion_meta WHERE item_key=?",
            ("SHORTNOTE",)
        ).fetchone()
    assert row is not None
    assert row["abstract"] is None
    assert row["abstract_source"] == "none"


def test_materialize_note_bundle_extractive_summary_long_note(tmp_path: Path):
    """Notes >= 100 words produce an extractive lead paragraph (no LLM call)."""
    from zocrates.rag.document_processor import materialize_note_bundle, _open_meta_db
    from zocrates.rag.zotero_db import ZoteroNote

    # Build a note with > 100 plain-text words
    long_body = " ".join([f"word{i}" for i in range(110)])
    long_html = f"<p>{long_body}</p>"
    note = ZoteroNote(
        note_id=3,
        note_key="LONGNOTE1",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="Long Note",
        note_html=long_html,
    )

    cache_dir = tmp_path / "cache2"
    cache_dir.mkdir()

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.get_metadata.return_value = None
    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor

    note_dir, _ = materialize_note_bundle(note, mock_syncer, cache_dir)

    with _open_meta_db(cache_dir) as conn:
        row = conn.execute(
            "SELECT abstract, abstract_source FROM conversion_meta WHERE item_key=?",
            ("LONGNOTE1",)
        ).fetchone()
    assert row is not None
    assert row["abstract_source"] == "extractive"
    assert row["abstract"] is not None
    assert len(row["abstract"]) <= 301  # 300 chars + possible ellipsis char


def test_materialize_note_bundle_staleness_invalidation(tmp_path: Path):
    """When HTML changes, materialize_note_bundle rebuilds the bundle."""
    from zocrates.rag.document_processor import materialize_note_bundle
    from zocrates.rag.zotero_db import ZoteroNote

    original_html = "<p>Original content.</p>"
    changed_html = "<p>Updated content after edit.</p>"

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    def _make_syncer(html_for_meta_return: str):
        old_hash = hashlib.sha256(original_html.encode()).hexdigest()
        new_hash = hashlib.sha256(html_for_meta_return.encode()).hexdigest()
        mock_processor = MagicMock()
        mock_processor._cache_dir = cache_dir
        # First call returns old hash (stale), second returns new hash
        mock_processor.get_metadata.return_value = {"source_hash": old_hash}
        mock_syncer = MagicMock()
        mock_syncer.processor = mock_processor
        return mock_syncer

    note = ZoteroNote(
        note_id=4,
        note_key="STALE001",
        parent_item_id=None,
        parent_item_key=None,
        title="Staleness Test",
        note_html=changed_html,
    )

    syncer = _make_syncer(original_html)
    # The existing hash doesn't match changed_html hash → should rebuild
    note_dir, key = materialize_note_bundle(note, syncer, cache_dir)
    doc = (note_dir / "document.md").read_text(encoding="utf-8")
    assert "Updated content after edit." in doc


# ---------------------------------------------------------------------------
# 10. _resolve_bundle_dir priority ordering
# ---------------------------------------------------------------------------


def test_resolve_bundle_dir_pdf_priority(tmp_path: Path):
    """PDF attachment is chosen over HTML when multiple converted attachments exist."""
    from zocrates.agents.common.toolkit import _resolve_bundle_dir
    from zocrates.rag.zotero_db import ZoteroAttachment, ZoteroNote

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # Create bundles: html first, pdf second
    html_dir = cache_dir / "HTMLATT"
    html_dir.mkdir()
    (html_dir / "document.md").write_text("HTML doc", encoding="utf-8")

    pdf_dir = cache_dir / "PDFATT"
    pdf_dir.mkdir()
    (pdf_dir / "document.md").write_text("PDF doc", encoding="utf-8")

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir

    html_att = MagicMock(spec=ZoteroAttachment)
    html_att.item_key = "HTMLATT"
    html_att.content_type = "text/html"
    html_att.path = MagicMock()
    html_att.path.name = "article.html"
    html_att.title = "HTML Snapshot"

    pdf_att = MagicMock(spec=ZoteroAttachment)
    pdf_att.item_key = "PDFATT"
    pdf_att.content_type = "application/pdf"
    pdf_att.path = MagicMock()
    pdf_att.path.name = "paper.pdf"
    pdf_att.title = "Full Text PDF"

    mock_item = MagicMock()
    mock_item.item_id = 10

    mock_db = MagicMock()
    mock_db.get_note_by_key.return_value = None
    mock_db.get_item_by_key.return_value = mock_item
    mock_db.get_attachments.return_value = [html_att, pdf_att]  # HTML listed first
    mock_db.get_notes_for_item.return_value = []

    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor
    mock_syncer.db = mock_db

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        resolved = _resolve_bundle_dir("PARENT99")

    assert resolved is not None
    bundle_dir, resolved_key, notice = resolved
    # PDF must win despite HTML being listed first in the return value
    assert resolved_key == "PDFATT"
    assert "HTMLATT" in notice  # HTML is in the disambiguation notice


def test_materialize_note_bundle_fast_path_unchanged(tmp_path: Path):
    """When bundle exists and source_hash matches, return immediately without re-writing."""
    from zocrates.rag.document_processor import materialize_note_bundle
    from zocrates.rag.zotero_db import ZoteroNote

    note = ZoteroNote(
        note_id=5,
        note_key="FASTPATH1",
        parent_item_id=None,
        parent_item_key=None,
        title="Fast Path Test",
        note_html="<p>Consistent content.</p>",
    )

    cache_dir = tmp_path / "cache_fast"
    cache_dir.mkdir()
    note_dir = cache_dir / "FASTPATH1"
    note_dir.mkdir()
    (note_dir / "document.md").write_text("pre-existing document.md", encoding="utf-8")

    expected_hash = hashlib.sha256(note.note_html.encode("utf-8")).hexdigest()

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.get_metadata.return_value = {"source_hash": expected_hash}

    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor

    resolved_dir, resolved_key = materialize_note_bundle(note, mock_syncer, cache_dir)
    assert resolved_key == "FASTPATH1"
    assert resolved_dir == note_dir
    # Content must NOT have been overwritten
    assert (note_dir / "document.md").read_text(encoding="utf-8") == "pre-existing document.md"


def test_resolve_bundle_dir_notes_only_fallback_notice(tmp_path: Path):
    """Parent item with 0 converted attachments and multiple notes returns first note + notice."""
    from zocrates.agents.common.toolkit import _resolve_bundle_dir, get_document_toc_core
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "cache_notes"
    cache_dir.mkdir()

    note1 = ZoteroNote(
        note_id=1,
        note_key="NOTE001",
        parent_item_id=10,
        parent_item_key="PARENT1",
        title="Primary Note",
        note_html="<p>Primary note content.</p>",
    )
    note2 = ZoteroNote(
        note_id=2,
        note_key="NOTE002",
        parent_item_id=10,
        parent_item_key="PARENT1",
        title="Secondary Note",
        note_html="<p>Secondary note content.</p>",
    )

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.get_metadata.return_value = None

    mock_item = MagicMock()
    mock_item.item_id = 10

    mock_db = MagicMock()
    mock_db.get_note_by_key.side_effect = lambda k: {"NOTE001": note1, "NOTE002": note2}.get(k)
    mock_db.get_item_by_key.return_value = mock_item
    mock_db.get_attachments.return_value = []  # No attachments
    mock_db.get_notes_for_item.return_value = [note1, note2]

    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor
    mock_syncer.db = mock_db

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = get_document_toc_core("PARENT1")

    assert res["status"] == "success"
    assert res["item_key"] == "NOTE001"
    assert "notice" in res
    assert "Loaded note 'Primary Note'" in res["notice"]
    assert "NOTE002" in res["notice"]


# ---------------------------------------------------------------------------
# 11. LibrarySyncer note bundle integration
# ---------------------------------------------------------------------------


def _make_syncer_stub(cache_dir: Path) -> "object":
    """Build a minimal LibrarySyncer stub with mocked db/processor/store."""
    from zocrates.rag.library_syncer import LibrarySyncer

    syncer = LibrarySyncer.__new__(LibrarySyncer)
    syncer.db = MagicMock()
    syncer.processor = MagicMock()
    syncer.processor._cache_dir = cache_dir
    syncer.store = MagicMock()
    return syncer


@pytest.mark.asyncio
async def test_ingest_item_materializes_child_note_bundle(tmp_path: Path):
    """_ingest_item calls materialize_note_bundle for child notes without a bundle."""
    from zocrates.rag.library_syncer import materialize_note_bundle
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir()
    syncer = _make_syncer_stub(cache_dir)
    syncer.db.get_attachments.return_value = []
    note = ZoteroNote(
        note_id=1,
        note_key="CHILDN01",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="Child Note",
        note_html="<p>Child note content.</p>",
    )
    syncer.db.get_notes_for_item.return_value = [note]
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        return_value=(cache_dir / "CHILDN01", "CHILDN01"),
    ) as mock_materialize:
        result = await syncer._ingest_item(10, "PARENT10", "Paper Title")

    assert result == "processed"
    mock_materialize.assert_called_once()
    args, kwargs = mock_materialize.call_args
    assert args[0] is note
    assert args[1] is syncer


@pytest.mark.asyncio
async def test_ingest_item_skips_materialization_when_bundle_exists(tmp_path: Path):
    """_ingest_item does not re-materialize an existing non-stale note bundle."""
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "corpus"
    note_dir = cache_dir / "CHILDN02"
    note_dir.mkdir(parents=True)
    (note_dir / "document.md").write_text("existing", encoding="utf-8")

    syncer = _make_syncer_stub(cache_dir)
    syncer.db.get_attachments.return_value = []
    note = ZoteroNote(
        note_id=2,
        note_key="CHILDN02",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="Child Note",
        note_html="<p>Content.</p>",
    )
    syncer.db.get_notes_for_item.return_value = [note]
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        return_value=(note_dir, "CHILDN02"),
    ) as mock_materialize:
        result = await syncer._ingest_item(10, "PARENT10", "Paper Title")

    assert result == "processed"
    mock_materialize.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_item_force_rematerializes_existing_bundle(tmp_path: Path):
    """force=True triggers materialize_note_bundle even when the bundle exists."""
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "corpus"
    note_dir = cache_dir / "CHILDN03"
    note_dir.mkdir(parents=True)
    (note_dir / "document.md").write_text("existing", encoding="utf-8")

    syncer = _make_syncer_stub(cache_dir)
    syncer.db.get_attachments.return_value = []
    note = ZoteroNote(
        note_id=3,
        note_key="CHILDN03",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="Child Note",
        note_html="<p>Content.</p>",
    )
    syncer.db.get_notes_for_item.return_value = [note]
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        return_value=(note_dir, "CHILDN03"),
    ) as mock_materialize:
        result = await syncer._ingest_item(10, "PARENT10", "Paper Title", force=True)

    assert result == "processed"
    mock_materialize.assert_called_once()


@pytest.mark.asyncio
async def test_ingest_standalone_note_materializes_bundle(tmp_path: Path):
    """_ingest_standalone_note calls materialize_note_bundle when no bundle exists."""
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir()
    syncer = _make_syncer_stub(cache_dir)
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None
    note = ZoteroNote(
        note_id=4,
        note_key="STANDN01",
        parent_item_id=None,
        parent_item_key=None,
        title="Standalone Note",
        note_html="<p>Standalone content.</p>",
    )

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        return_value=(cache_dir / "STANDN01", "STANDN01"),
    ) as mock_materialize:
        result = await syncer._ingest_standalone_note(note)

    assert result == "processed"
    mock_materialize.assert_called_once()
    args, kwargs = mock_materialize.call_args
    assert args[0] is note
    assert args[1] is syncer


@pytest.mark.asyncio
async def test_ingest_standalone_note_skips_materialization_when_bundle_exists(tmp_path: Path):
    """_ingest_standalone_note skips materialization for an existing bundle (non-force)."""
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "corpus"
    note_dir = cache_dir / "STANDN02"
    note_dir.mkdir(parents=True)
    (note_dir / "document.md").write_text("existing", encoding="utf-8")

    syncer = _make_syncer_stub(cache_dir)
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None
    note = ZoteroNote(
        note_id=5,
        note_key="STANDN02",
        parent_item_id=None,
        parent_item_key=None,
        title="Standalone Note",
        note_html="<p>Standalone content.</p>",
    )

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        return_value=(note_dir, "STANDN02"),
    ) as mock_materialize:
        result = await syncer._ingest_standalone_note(note)

    assert result == "processed"
    mock_materialize.assert_not_called()


# ---------------------------------------------------------------------------
# 12. scripts/materialize_notes.py
# ---------------------------------------------------------------------------


def test_materialize_notes_script_materializes_all_notes(tmp_path: Path):
    """materialize_all_notes initializes services and materializes every note."""
    from zocrates.rag.zotero_db import ZoteroNote

    zotero_dir = tmp_path / "Zotero"
    zotero_dir.mkdir()

    notes = [
        ZoteroNote(
            note_id=1,
            note_key="NOTEAAA",
            parent_item_id=None,
            parent_item_key=None,
            title="Note A",
            note_html="<p>Note A content.</p>",
        ),
        ZoteroNote(
            note_id=2,
            note_key="NOTEBBB",
            parent_item_id=None,
            parent_item_key=None,
            title="Note B",
            note_html="<p>Note B content.</p>",
        ),
    ]

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = zotero_dir / "zocrates-data" / "corpus"
    mock_syncer.db.get_all_notes.return_value = notes

    with (
        patch("scripts.materialize_notes.initialize_services") as mock_init,
        patch("scripts.materialize_notes.get_syncer", return_value=mock_syncer) as mock_get,
        patch(
            "scripts.materialize_notes.materialize_note_bundle",
            side_effect=lambda note, syncer: (syncer.processor._cache_dir / note.note_key, note.note_key),
        ) as mock_materialize,
    ):
        from scripts.materialize_notes import materialize_all_notes

        result = materialize_all_notes(zotero_dir)

    assert result == 2
    mock_init.assert_called_once_with(zotero_dir)
    mock_get.assert_called_once()
    assert mock_materialize.call_count == 2


def test_materialize_notes_script_empty_library(tmp_path: Path):
    """materialize_all_notes returns 0 and calls no materialization for an empty library."""
    mock_syncer = MagicMock()
    mock_syncer.db.get_all_notes.return_value = []

    with (
        patch("scripts.materialize_notes.initialize_services"),
        patch("scripts.materialize_notes.get_syncer", return_value=mock_syncer),
        patch("scripts.materialize_notes.materialize_note_bundle") as mock_materialize,
    ):
        from scripts.materialize_notes import materialize_all_notes

        result = materialize_all_notes(tmp_path / "Zotero")

    assert result == 0
    mock_materialize.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_item_materialization_failure_does_not_block_vector_ingestion(tmp_path: Path):
    """When materialize_note_bundle raises, child note vector ingestion still succeeds."""
    from zocrates.rag.zotero_db import ZoteroNote

    note = ZoteroNote(
        note_id=99,
        note_key="FAILNOTE1",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="Note with failed materialization",
        note_html="<p>This note still gets vector indexed.</p>",
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    syncer = _make_syncer_stub(cache_dir)
    syncer.db.get_attachments.return_value = []
    syncer.db.get_notes_for_item.return_value = [note]
    syncer.store.is_ingested.return_value = False
    syncer.store.ingest_document.return_value = None

    with patch(
        "zocrates.rag.library_syncer.materialize_note_bundle",
        side_effect=RuntimeError("Disk failure"),
    ) as mock_materialize:
        status = await syncer._ingest_item(10, "PARENT10", "Parent Title")

    assert status == "processed"
    mock_materialize.assert_called_once()
    # Vector store must still have received the document
    syncer.store.ingest_document.assert_called_once()
    args, kwargs = syncer.store.ingest_document.call_args
    assert args[0] == "FAILNOTE1"  # note_key


@pytest.mark.asyncio
async def test_ingest_standalone_note_force_rematerializes_existing_bundle(tmp_path: Path):
    """When force=True, standalone note re-materializes even if bundle exists."""
    from zocrates.rag.zotero_db import ZoteroNote

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    bundle_dir = cache_dir / "STANDNOTE2"
    bundle_dir.mkdir()
    (bundle_dir / "document.md").write_text("existing", encoding="utf-8")

    note = ZoteroNote(
        note_id=88,
        note_key="STANDNOTE2",
        parent_item_id=None,
        parent_item_key=None,
        title="Standalone",
        note_html="<p>Standalone text.</p>",
    )
    syncer = _make_syncer_stub(cache_dir)

    with patch("zocrates.rag.library_syncer.materialize_note_bundle") as mock_materialize:
        await syncer._ingest_standalone_note(note, force=True)

    mock_materialize.assert_called_once_with(note, syncer)


def test_extract_compact_toc_outline_normal_and_level_folding(tmp_path: Path):
    from zocrates.agents.common.toolkit import extract_compact_toc_outline

    bundle_dir = tmp_path / "bundle_normal"
    bundle_dir.mkdir()

    # 1. Normal document (<= 15 sections, <= 8 figures, <= 6 tables)
    toc_data = {
        "title": "Attention Is All You Need",
        "item_key": "ATTKEY1",
        "sections": [
            {"id": "s0", "heading": "Abstract", "level": 0, "word_count": 200},
            {"id": "s1", "heading": "Introduction", "level": 1, "word_count": 500},
            {"id": "s2", "heading": "Model Architecture", "level": 1, "word_count": 1200},
            {"id": "s3", "heading": "Attention Details", "level": 2, "word_count": 400},
        ],
        "figures": [
            {"id": "f1", "caption": "Transformer architecture"},
            {"id": "f2", "caption": "Scaled dot-product attention"},
        ],
        "tables": [
            {"id": "t1", "caption": "BLEU score benchmarks"},
        ],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    outline = extract_compact_toc_outline(bundle_dir, "Attention Is All You Need", "ATTKEY1", parent_key="PARENT1")
    assert "### [Key: ATTKEY1, Parent Key: PARENT1] \"Attention Is All You Need\"" in outline
    assert "- [s0] Abstract (~200 words)" in outline
    assert "- [s1] Introduction (~500 words)" in outline
    assert "- [s2] Model Architecture (~1200 words)" in outline
    assert "- [s3] Attention Details (~400 words)" in outline
    assert "- Figures:\n  - [f1] Transformer architecture\n  - [f2] Scaled dot-product attention" in outline
    assert "- Tables:\n  - [t1] BLEU score benchmarks" in outline

    # 2. Dense document (> 15 sections) triggers level-folding (omits level > 2)
    bundle_dense = tmp_path / "bundle_dense"
    bundle_dense.mkdir()
    dense_sections = [{"id": f"s{i}", "heading": f"Section {i}", "level": 1 if i < 10 else 3, "word_count": 100} for i in range(20)]
    dense_figures = [{"id": f"f{i+1}", "caption": f"Figure {i+1}"} for i in range(12)]
    dense_tables = [{"id": f"t{i+1}", "caption": f"Table {i+1}"} for i in range(8)]
    dense_data = {
        "title": "Comprehensive Survey",
        "item_key": "SURVEY1",
        "sections": dense_sections,
        "figures": dense_figures,
        "tables": dense_tables,
    }
    (bundle_dense / "toc.json").write_text(json.dumps(dense_data), encoding="utf-8")

    dense_outline = extract_compact_toc_outline(bundle_dense, "Comprehensive Survey", "SURVEY1")
    assert "### [Key: SURVEY1] \"Comprehensive Survey\"" in dense_outline
    assert "- [s0] Section 0 (~100 words)" in dense_outline
    assert "- [s9] Section 9 (~100 words)" in dense_outline
    assert "Section 10" not in dense_outline  # level 3 folded away
    # Dense figures/tables summarized by complete range
    assert "12 figures present ([f1] to [f12])" in dense_outline
    assert "8 tables present ([t1] to [t8])" in dense_outline

    # 3. Document with abstract in metadata but no explicit abstract section
    bundle_meta_abs = tmp_path / "bundle_meta_abs"
    bundle_meta_abs.mkdir()
    meta_abs_data = {
        "title": "Paper Without Abstract Section",
        "item_key": "NOABS1",
        "abstract": "This is a substantive summary of the paper extracted from Zotero metadata.",
        "abstract_source": "zotero",
        "sections": [
            {"id": "s0", "heading": "Frontmatter", "level": 0, "word_count": 30},
            {"id": "s1", "heading": "Introduction", "level": 1, "word_count": 400},
        ],
    }
    (bundle_meta_abs / "toc.json").write_text(json.dumps(meta_abs_data), encoding="utf-8")
    meta_outline = extract_compact_toc_outline(bundle_meta_abs, "Paper Without Abstract Section", "NOABS1")
    assert "- [abstract] Abstract / Summary (~12 words)" in meta_outline
    assert "- [s0] Frontmatter (~30 words)" in meta_outline
    assert "- [s1] Introduction (~400 words)" in meta_outline

    # 4. Document with both metadata abstract and explicit abstract section (no duplicate emission)
    bundle_both = tmp_path / "bundle_both"
    bundle_both.mkdir()
    both_data = {
        "title": "Paper With Both",
        "item_key": "BOTH1",
        "abstract": "Metadata abstract text here.",
        "abstract_source": "zotero",
        "sections": [
            {"id": "s0", "heading": "Frontmatter", "level": 0, "word_count": 30},
            {"id": "s1", "heading": "Abstract", "level": 1, "word_count": 150},
            {"id": "s2", "heading": "Introduction", "level": 1, "word_count": 400},
        ],
    }
    (bundle_both / "toc.json").write_text(json.dumps(both_data), encoding="utf-8")
    both_outline = extract_compact_toc_outline(bundle_both, "Paper With Both", "BOTH1")
    assert "- [s1] Abstract (~150 words)" in both_outline
    assert "- [abstract]" not in both_outline


def test_extract_compact_toc_outline_fallback_to_toc_md(tmp_path: Path):
    from zocrates.agents.common.toolkit import extract_compact_toc_outline

    bundle_dir = tmp_path / "bundle_md_only"
    bundle_dir.mkdir()
    toc_md = """# Fallback Paper

## Abstract
Long abstract text that should be excluded from compact outline.

## Table of Contents
- [s0] Abstract (~150 words)
- [s1] Background (~400 words)

## Figures
- [f1] System diagram

## Tables
- [t1] Results
"""
    (bundle_dir / "toc.md").write_text(toc_md, encoding="utf-8")

    outline = extract_compact_toc_outline(bundle_dir, "Fallback Paper", "FALL1")
    assert "### [Key: FALL1] \"Fallback Paper\"" in outline
    assert "Long abstract text that should be excluded" not in outline
    assert "- [s0] Abstract (~150 words)" in outline
    assert "- [s1] Background (~400 words)" in outline


def test_prune_cached_tocs_lru_bounding():
    from zocrates.agents.common.toolkit import _prune_cached_tocs

    cached_tocs = {}
    # Insert 15 unique document bundles, each with parent key and attachment key
    for i in range(15):
        p_key = f"PARENT_{i}"
        a_key = f"ATT_{i}"
        entry = {
            "input_key": p_key,
            "resolved_key": a_key,
            "title": f"Paper {i}",
            "outline_text": f"Outline {i}",
            "updated_at": 1000 + i,  # i=0 oldest, i=14 newest
        }
        cached_tocs[p_key] = entry
        cached_tocs[a_key] = entry

    assert len(cached_tocs) == 30  # 15 * 2 alias keys
    pruned = _prune_cached_tocs(cached_tocs, max_entries=10)

    # Should retain 10 newest (i=5..14), pruned 5 oldest (i=0..4)
    for i in range(5):
        assert f"PARENT_{i}" not in pruned
        assert f"ATT_{i}" not in pruned
    for i in range(5, 15):
        assert f"PARENT_{i}" in pruned
        assert f"ATT_{i}" in pruned
    assert len(pruned) == 20  # 10 unique bundles * 2 alias keys


def test_get_document_toc_populates_dual_key_cache():
    from zocrates.agents.common.state_keys import CACHED_TOCS
    from zocrates.agents.common.tools import get_document_toc

    mock_ctx = MockToolContext(
        {
            CACHED_TOCS: {},
            TEMP_RETRIEVAL_TOOL_COUNT: 0,
        }
    )

    fake_toc = "## Table of Contents\n- [s0] Abstract\n- [s1] Methods"
    with patch(
        "zocrates.agents.common.tools.get_document_toc_core",
        return_value={"status": "success", "item_key": "ATT_RES_1", "toc_content": fake_toc, "title": "Test Title"},
    ):
        res = get_document_toc("PARENT_INPUT_1", tool_context=mock_ctx)
        assert res["status"] == "success"

    state_cache = mock_ctx.state[CACHED_TOCS]
    assert "PARENT_INPUT_1" in state_cache
    assert "ATT_RES_1" in state_cache
    entry = state_cache["PARENT_INPUT_1"]
    assert entry["input_key"] == "PARENT_INPUT_1"
    assert entry["resolved_key"] == "ATT_RES_1"
    assert '### [Key: ATT_RES_1, Parent Key: PARENT_INPUT_1] "Test Title"' in entry["outline_text"]
    assert "- [s0] Abstract" in entry["outline_text"]


def test_search_corpus_core_liveness_filtering():
    """search_corpus_core filters out deleted items and retains active items."""
    mock_syncer = MagicMock()
    mock_store = MagicMock()
    mock_db = MagicMock()
    mock_syncer.store = mock_store
    mock_syncer.db = mock_db

    res_active = SearchResult(
        item_id=1,
        item_key="ACTIVE1",
        chunk_index=0,
        chunk_text="Active text",
        heading_path=[],
        score=0.1,
        raw_semantic_score=0.9,
        raw_keyword_rank=1,
        retrieval_method="hybrid",
        chunk_type="text",
        image_path=None,
        caption_source=None,
        vlm_model=None,
        chunk_id="ACTIVE1::0",
        line_start=1,
        line_end=10,
    )
    res_deleted = SearchResult(
        item_id=2,
        item_key="DELETED1",
        chunk_index=0,
        chunk_text="Deleted text",
        heading_path=[],
        score=0.1,
        raw_semantic_score=0.9,
        raw_keyword_rank=2,
        retrieval_method="hybrid",
        chunk_type="text",
        image_path=None,
        caption_source=None,
        vlm_model=None,
        chunk_id="DELETED1::0",
        line_start=1,
        line_end=10,
    )
    mock_store.search.return_value = [res_active, res_deleted]
    mock_db.get_active_item_keys.return_value = {"ACTIVE1"}

    mock_active_item = MagicMock()
    mock_active_item.title = "Active Title"
    mock_db.get_item.side_effect = lambda item_id: mock_active_item if item_id == 1 else None
    mock_db.get_item_by_key.side_effect = lambda key: mock_active_item if key == "ACTIVE1" else None

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = search_corpus_core("test query")

    assert res["status"] == "success"
    assert len(res["results"]) == 1
    assert res["results"][0]["item_key"] == "ACTIVE1"
    assert res["results"][0]["title"] == "Active Title"


def test_search_corpus_core_preserves_active_item_with_empty_title():
    """search_corpus_core retains active items even if title is empty."""
    mock_syncer = MagicMock()
    mock_store = MagicMock()
    mock_db = MagicMock()
    mock_syncer.store = mock_store
    mock_syncer.db = mock_db

    res_active = SearchResult(
        item_id=1,
        item_key="ACTIVE_UNTITLED",
        chunk_index=0,
        chunk_text="Some text without title",
        heading_path=[],
        score=0.1,
        raw_semantic_score=0.9,
        raw_keyword_rank=1,
        retrieval_method="hybrid",
        chunk_type="text",
        image_path=None,
        caption_source=None,
        vlm_model=None,
        chunk_id="ACTIVE_UNTITLED::0",
        line_start=1,
        line_end=10,
    )
    mock_store.search.return_value = [res_active]
    mock_db.get_active_item_keys.return_value = {"ACTIVE_UNTITLED"}

    mock_active_item = MagicMock()
    mock_active_item.title = ""
    mock_db.get_item.side_effect = lambda item_id: mock_active_item if item_id == 1 else None
    mock_db.get_item_by_key.side_effect = lambda key: mock_active_item if key == "ACTIVE_UNTITLED" else None

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = search_corpus_core("test query")

    assert res["status"] == "success"
    assert len(res["results"]) == 1
    assert res["results"][0]["item_key"] == "ACTIVE_UNTITLED"
    assert res["results"][0]["title"] == "Untitled Document"


def test_resolve_bundle_dir_inactive_note_rejection(tmp_path: Path):
    """_resolve_bundle_dir rejects inactive/parent-deleted notes before materialize_note_bundle."""
    from zocrates.agents.common.toolkit import _resolve_bundle_dir

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_db = MagicMock()
    mock_note = MagicMock()
    mock_note.note_key = "DELNOTE"
    mock_db.get_note_by_key.return_value = mock_note
    mock_db.is_item_active.return_value = False
    mock_syncer.db = mock_db

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer), \
         patch("zocrates.rag.document_processor.materialize_note_bundle") as mock_materialize:
        resolved = _resolve_bundle_dir("DELNOTE")

    assert resolved is None
    mock_materialize.assert_not_called()


def test_resolve_bundle_dir_inactive_item_rejection(tmp_path: Path):
    """_resolve_bundle_dir refuses to return a disk bundle if is_item_active is False."""
    from zocrates.agents.common.toolkit import _resolve_bundle_dir

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    del_dir = cache_dir / "DEL123"
    del_dir.mkdir()
    (del_dir / "document.md").write_text("deleted content", encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_db = MagicMock()
    mock_db.get_note_by_key.return_value = None
    mock_db.is_item_active.return_value = False
    mock_syncer.db = mock_db

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        resolved = _resolve_bundle_dir("DEL123")
    assert resolved is None


def test_get_document_toc_evicts_deleted_item_from_cache():
    """get_document_toc evicts inactive items from cached_tocs and returns error."""
    from zocrates.agents.common.state_keys import CACHED_TOCS
    from zocrates.agents.common.tools import get_document_toc

    mock_ctx = MockToolContext(
        {
            CACHED_TOCS: {
                "DEL_ITEM": {
                    "input_key": "DEL_ITEM",
                    "resolved_key": "DEL_ITEM",
                    "title": "Deleted Title",
                    "outline_text": "### Deleted Outline",
                    "updated_at": 1000,
                }
            },
            TEMP_RETRIEVAL_TOOL_COUNT: 0,
        }
    )

    mock_syncer = MagicMock()
    mock_db = MagicMock()
    mock_db.is_item_active.return_value = False
    mock_syncer.db = mock_db

    with patch("zocrates.agents.common.tools._resolve_bundle_dir", return_value=None), \
         patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = get_document_toc("DEL_ITEM", tool_context=mock_ctx)

    assert res["status"] == "error"
    assert "deleted" in res["error"].lower()
    assert "DEL_ITEM" not in mock_ctx.state[CACHED_TOCS]


def test_get_document_toc_populates_abstract_in_temp_evidence(tmp_path: Path):
    """get_document_toc extracts abstract and accumulates it into TEMP_RETRIEVED_EVIDENCE."""
    import json
    from zocrates.agents.common.state_keys import CACHED_TOCS, TEMP_RETRIEVED_EVIDENCE
    from zocrates.agents.common.tools import get_document_toc

    bundle_dir = tmp_path / "bundle_item_1"
    bundle_dir.mkdir()
    toc_data = {
        "item_key": "ITEM_1",
        "title": "Protein Paper",
        "abstract": "Proteins are vital molecules performing cellular functions.",
        "abstract_source": "zotero",
        "sections": [{"id": "s0", "heading": "Frontmatter"}],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")
    (bundle_dir / "toc.md").write_text("# Protein Paper\n\n## Abstract\nProteins are vital molecules performing cellular functions.\n\n## Table of Contents\n- [s0] Frontmatter", encoding="utf-8")

    mock_ctx = MockToolContext(
        {
            CACHED_TOCS: {},
            TEMP_RETRIEVED_EVIDENCE: [],
            TEMP_RETRIEVAL_TOOL_COUNT: 0,
        }
    )

    fake_toc = "# Protein Paper\n\n## Abstract\nProteins are vital molecules performing cellular functions.\n\n## Table of Contents\n- [s0] Frontmatter"
    with patch("zocrates.agents.common.tools._resolve_bundle_dir", return_value=(bundle_dir, "ITEM_1", None)), \
         patch("zocrates.agents.common.tools.get_document_toc_core", return_value={"status": "success", "item_key": "ITEM_1", "toc_content": fake_toc, "title": "Protein Paper"}):
        res = get_document_toc("ITEM_1", tool_context=mock_ctx)

    assert res["status"] == "success"
    evidence = mock_ctx.state[TEMP_RETRIEVED_EVIDENCE]
    assert len(evidence) == 1
    chunk = evidence[0]
    assert chunk["chunk_id"] == "ITEM_1:abstract"
    assert chunk["heading"] == "Abstract"
    assert "Proteins are vital" in chunk["text"]
    assert chunk["word_count"] > 0
