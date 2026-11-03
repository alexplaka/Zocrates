"""Unit tests for substantive evidence filtering, reranking exclusion, and academic metadata formatting."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zocrates.agents.common.prompts import format_retrieved_evidence_str
from zocrates.agents.common.reranker import (
    filter_substantive_evidence,
    is_substantive_chunk,
    rerank_evidence,
)
from zocrates.agents.common.state_keys import (
    ACTIVE_CONTEXT_ITEMS,
    TEMP_RETRIEVED_EVIDENCE,
    TEMP_SEEN_ASSETS,
    TEMP_SEEN_CHUNK_IDS,
)
from zocrates.agents.common.toolkit import read_document_core, search_corpus_core
from zocrates.agents.common.tools import read_document
from zocrates.rag.vector_store import SearchResult


# ---------------------------------------------------------------------------
# 1. is_substantive_chunk Tests
# ---------------------------------------------------------------------------


def test_is_substantive_chunk_frontmatter_and_toc():
    """Verify that frontmatter chunks, :s0 chunks, TOC headings, and YAML frontmatter are flagged non-substantive."""
    # chunk_type == "frontmatter"
    assert not is_substantive_chunk({"chunk_type": "frontmatter", "text": "Some text"})

    # chunk_id ending in :s0
    assert not is_substantive_chunk({"chunk_id": "ITEM123::s0", "chunk_type": "text"})
    assert not is_substantive_chunk({"chunk_id": "ITEM123:s0", "chunk_type": "text"})

    # asset_id == "s0"
    assert not is_substantive_chunk({"asset_id": "s0", "chunk_type": "text"})

    # Headings matching frontmatter / TOC variants
    assert not is_substantive_chunk({"heading": "Frontmatter", "chunk_type": "text"})
    assert not is_substantive_chunk({"heading": "Table of Contents", "chunk_type": "text"})
    assert not is_substantive_chunk({"heading": "contents", "chunk_type": "text"})
    assert not is_substantive_chunk({"heading": "TOC", "chunk_type": "text"})
    assert not is_substantive_chunk({"heading": "  Table of Contents  ", "chunk_type": "text"})

    # Pure YAML frontmatter in text
    pure_yaml = "---\ntitle: Sample Paper\nauthors:\n  - Alice Smith\nyear: 2024\n---\n"
    assert not is_substantive_chunk({"chunk_type": "text", "text": pure_yaml})

    yaml_title_only = "---\ntitle: Just a Title\n---\n"
    assert not is_substantive_chunk({"chunk_type": "text", "text": yaml_title_only})

    yaml_authors_only = "---\nauthors:\n  - Bob Jones\n---\n"
    assert not is_substantive_chunk({"chunk_type": "text", "text": yaml_authors_only})


def test_is_substantive_chunk_substantive_content():
    """Verify that normal text, table, figure, and substantive section chunks return True."""
    # Normal text chunk
    assert is_substantive_chunk({
        "chunk_id": "ITEM123::1",
        "chunk_type": "text",
        "heading": "1. Introduction",
        "text": "Deep learning architectures have revolutionized NLP.",
    })

    # Normal figure chunk
    assert is_substantive_chunk({
        "chunk_id": "ITEM123::f1",
        "asset_id": "f1",
        "chunk_type": "figure",
        "heading": "Architecture Diagram",
        "image_path": "images/fig1.png",
        "text": "A schematic of the neural network layers.",
    })

    # Normal table chunk
    assert is_substantive_chunk({
        "chunk_id": "ITEM123::t1",
        "asset_id": "t1",
        "chunk_type": "table",
        "heading": "Ablation Study",
        "structured_content": "model,accuracy\nbaseline,85.2\nours,91.4",
    })

    # Heading containing substring like 'content' but not TOC
    assert is_substantive_chunk({
        "chunk_id": "ITEM123::4",
        "chunk_type": "text",
        "heading": "Content Analysis and Thematic Coding",
        "text": "Qualitative analysis of interview transcripts was conducted.",
    })


# ---------------------------------------------------------------------------
# 2. filter_substantive_evidence Tests
# ---------------------------------------------------------------------------


def test_filter_substantive_evidence_drops_frontmatter_when_substantive_exist():
    """filter_substantive_evidence strips frontmatter chunks when > 2 substantive chunks remain."""
    chunks = [
        {"chunk_id": "P1::s0", "chunk_type": "frontmatter", "text": "---\ntitle: P1\n---"},
        {"chunk_id": "P1::1", "chunk_type": "text", "heading": "Method", "text": "We ran simulations."},
        {"chunk_id": "P2::s0", "heading": "Table of Contents", "chunk_type": "text", "text": "1. Intro"},
        {"chunk_id": "P2::2", "chunk_type": "text", "heading": "Results", "text": "Accuracy reached 94%."},
        {"chunk_id": "P3::3", "chunk_type": "text", "heading": "Discussion", "text": "These findings suggest a novel approach."},
    ]

    filtered = filter_substantive_evidence(chunks)
    # 3 substantive chunks > 2, so frontmatter/TOC are stripped
    assert len(filtered) == 3
    assert [c["chunk_id"] for c in filtered] == ["P1::1", "P2::2", "P3::3"]


def test_filter_substantive_evidence_fallback_when_only_frontmatter():
    """Fallback guard: preserves original chunks when ONLY frontmatter chunks are provided."""
    only_fm = [
        {"chunk_id": "P1::s0", "chunk_type": "frontmatter", "text": "---\ntitle: P1\nauthors:\n  - A\n---"},
        {"chunk_id": "P2::s0", "heading": "TOC", "chunk_type": "text", "text": "---\ntitle: P2\n---"},
    ]

    filtered = filter_substantive_evidence(only_fm)
    assert len(filtered) == 2
    assert filtered == only_fm


def test_filter_substantive_evidence_fallback_when_two_or_fewer_substantive_chunks():
    """Fallback guard: when <= 2 substantive chunks exist alongside frontmatter, all chunks are preserved."""
    # 1 substantive + 1 frontmatter — should fall back to returning all 2
    chunks_one_sub = [
        {"chunk_id": "P1::s0", "chunk_type": "frontmatter", "text": "---\ntitle: P1\nauthors:\n  - A\n---"},
        {"chunk_id": "P1::1", "chunk_type": "text", "heading": "Introduction", "text": "One substantive chunk."},
    ]
    filtered = filter_substantive_evidence(chunks_one_sub)
    assert filtered == chunks_one_sub, "1 substantive chunk: expected fallback to all original chunks"

    # 2 substantive + 1 frontmatter — should still fall back to returning all 3
    chunks_two_sub = [
        {"chunk_id": "P2::s0", "chunk_type": "frontmatter", "text": "---\ntitle: P2\nauthors:\n  - B\n---"},
        {"chunk_id": "P2::1", "chunk_type": "text", "heading": "Method", "text": "First substantive chunk."},
        {"chunk_id": "P2::2", "chunk_type": "text", "heading": "Results", "text": "Second substantive chunk."},
    ]
    filtered = filter_substantive_evidence(chunks_two_sub)
    assert filtered == chunks_two_sub, "2 substantive chunks: expected fallback to all original chunks"

    # <= 2 substantive chunks with NO frontmatter — returns identical substantive list without logging
    chunks_no_fm = [
        {"chunk_id": "P3::1", "chunk_type": "text", "heading": "Method", "text": "First substantive chunk."},
        {"chunk_id": "P3::2", "chunk_type": "text", "heading": "Results", "text": "Second substantive chunk."},
    ]
    filtered_no_fm = filter_substantive_evidence(chunks_no_fm)
    assert filtered_no_fm == chunks_no_fm, "No frontmatter: expected exact substantive list returned"


def test_filter_substantive_evidence_empty():
    """Empty list returns empty list."""
    assert filter_substantive_evidence([]) == []


# ---------------------------------------------------------------------------
# 3. rerank_evidence Excludes Frontmatter Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_evidence_excludes_frontmatter_from_scoring():
    """rerank_evidence filters out frontmatter before sending candidate documents to OpenRouter
    when more than 2 substantive chunks exist."""
    chunks = [
        {
            "chunk_id": "FM::s0",
            "chunk_type": "frontmatter",
            "text": "---\ntitle: Excluded Paper\nauthors:\n  - Alice\n---\n",
            "word_count": 10,
        },
        {
            "chunk_id": "SUB::1",
            "chunk_type": "text",
            "heading": "Core Analysis",
            "text": "Substantive evidence text that should be scored.",
            "word_count": 8,
        },
        {
            "chunk_id": "SUB::2",
            "chunk_type": "text",
            "heading": "Empirical Results",
            "text": "Empirical results showing significant gains.",
            "word_count": 7,
        },
        {
            "chunk_id": "SUB::3",
            "chunk_type": "text",
            "heading": "Discussion",
            "text": "Discussion of the implications for future research.",
            "word_count": 9,
        },
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.88},
            {"index": 1, "relevance_score": 0.95},
            {"index": 2, "relevance_score": 0.75},
        ]
    }

    with patch(
        "zocrates.agents.common.reranker.get_secure_api_key",
        return_value="test-api-key",
    ), patch(
        "zocrates.agents.common.reranker.load_zocrates_config",
        return_value={"reranker_model": "test-reranker-model"},
    ), patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        ranked = await rerank_evidence("my research query", chunks)

        # Ensure httpx POST was called with only the 3 substantive documents (not frontmatter)
        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        sent_docs = call_kwargs["json"]["documents"]
        assert len(sent_docs) == 3
        assert "Substantive evidence text" in sent_docs[0]
        assert "Empirical results" in sent_docs[1]
        assert "Discussion of the implications" in sent_docs[2]
        assert "---\ntitle: Excluded Paper" not in sent_docs

        # Ensure ranked results exclude the frontmatter chunk
        assert len(ranked) == 3
        ranked_ids = [c["chunk_id"] for c in ranked]
        assert "FM::s0" not in ranked_ids
        assert ranked[0]["chunk_id"] == "SUB::2"  # highest score 0.95
        assert ranked[1]["chunk_id"] == "SUB::1"  # score 0.88
        assert ranked[2]["chunk_id"] == "SUB::3"  # score 0.75


@pytest.mark.asyncio
async def test_rerank_evidence_preserves_only_frontmatter_fallback():
    """When only frontmatter is provided, fallback preserves it without crashing."""
    only_fm = [
        {
            "chunk_id": "FM::s0",
            "chunk_type": "frontmatter",
            "text": "---\ntitle: Solo Paper\nauthors:\n  - Author\n---\n",
            "word_count": 10,
        }
    ]

    ranked = await rerank_evidence("query", only_fm)
    assert len(ranked) == 1
    assert ranked[0]["chunk_id"] == "FM::s0"


@pytest.mark.asyncio
async def test_rerank_evidence_keeps_frontmatter_when_two_or_fewer_substantive_chunks():
    """rerank_evidence keeps frontmatter in scoring pool when <= 2 substantive chunks exist."""
    chunks = [
        {
            "chunk_id": "FM::s0",
            "chunk_type": "frontmatter",
            "text": "---\ntitle: Included Paper\nauthors:\n  - Bob\n---\n",
            "word_count": 10,
        },
        {
            "chunk_id": "SUB::1",
            "chunk_type": "text",
            "heading": "Introduction",
            "text": "Only one substantive chunk retrieved.",
            "word_count": 6,
        },
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.8},
        ]
    }

    with patch(
        "zocrates.agents.common.reranker.get_secure_api_key",
        return_value="test-api-key",
    ), patch(
        "zocrates.agents.common.reranker.load_zocrates_config",
        return_value={"reranker_model": "test-reranker-model"},
    ), patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock
    ) as mock_post:
        mock_post.return_value = mock_response

        ranked = await rerank_evidence("query about paper", chunks)

        # Both chunks (including frontmatter) must be sent to the reranker
        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        sent_docs = call_kwargs["json"]["documents"]
        assert len(sent_docs) == 2
        assert any("Included Paper" in d for d in sent_docs), "frontmatter text should be in scoring pool"

        # Both chunks must appear in results
        assert len(ranked) == 2
        ranked_ids = [c["chunk_id"] for c in ranked]
        assert "FM::s0" in ranked_ids
        assert "SUB::1" in ranked_ids
        # SUB::1 scored 0.8 (index 1) → highest rank
        assert ranked[0]["chunk_id"] == "SUB::1"


# ---------------------------------------------------------------------------
# 4. format_retrieved_evidence_str Academic Metadata Tests
# ---------------------------------------------------------------------------


def test_format_retrieved_evidence_str_renders_authors_and_year():
    """format_retrieved_evidence_str renders clean authors and year attributes when present."""
    chunks = [
        # 1 author + year
        {
            "title": "Attention Is All You Need",
            "authors": ["Vaswani"],
            "year": "2017",
            "chunk_type": "text",
            "heading": "Abstract",
            "text": "We introduce the Transformer.",
        },
        # 2 authors + int year
        {
            "title": "BERT Pre-training",
            "authors": ["Devlin", "Chang"],
            "year": 2018,
            "chunk_type": "text",
            "heading": "Introduction",
            "text": "BERT is designed to pre-train deep representations.",
        },
        # >2 authors (et al.) + year
        {
            "title": "Deep Residual Learning",
            "authors": ["He", "Zhang", "Ren", "Sun"],
            "year": "2016",
            "chunk_type": "text",
            "heading": "Architecture",
            "text": "Residual networks are easier to optimize.",
        },
        # String authors + year
        {
            "title": "Single Author Study",
            "authors": "Turing",
            "year": "1950",
            "chunk_type": "text",
            "heading": "Computing Machinery",
            "text": "Can machines think?",
        },
        # Year "N/A" should be omitted
        {
            "title": "Undated Report",
            "authors": ["Smith"],
            "year": "N/A",
            "chunk_type": "text",
            "text": "Report without publication year.",
        },
        # Lowercase "n/a" year should be omitted
        {
            "title": "Undated Report 2",
            "authors": ["Jones"],
            "year": "n/a",
            "chunk_type": "text",
            "text": "Another undated report.",
        },
        # Absent authors and year (clean backward compatibility)
        {
            "title": "Legacy Chunk",
            "chunk_type": "text",
            "heading": "Legacy Section",
            "text": "Chunk without authors or year.",
        },
    ]

    formatted = format_retrieved_evidence_str(chunks)

    # 1 author: "Vaswani"
    assert '<chunk index="1" title="Attention Is All You Need" authors="Vaswani" year="2017" section="Abstract" type="text">' in formatted

    # 2 authors: "Devlin & Chang"
    assert '<chunk index="2" title="BERT Pre-training" authors="Devlin &amp; Chang" year="2018" section="Introduction" type="text">' in formatted

    # >2 authors: "He et al."
    assert '<chunk index="3" title="Deep Residual Learning" authors="He et al." year="2016" section="Architecture" type="text">' in formatted

    # String authors: "Turing"
    assert '<chunk index="4" title="Single Author Study" authors="Turing" year="1950" section="Computing Machinery" type="text">' in formatted

    # "N/A" year omitted
    assert '<chunk index="5" title="Undated Report" authors="Smith" type="text">' in formatted
    assert 'year="N/A"' not in formatted

    # "n/a" year omitted
    assert '<chunk index="6" title="Undated Report 2" authors="Jones" type="text">' in formatted
    assert 'year="n/a"' not in formatted

    # Absent authors and year omitted
    assert '<chunk index="7" title="Legacy Chunk" section="Legacy Section" type="text">' in formatted


def test_format_retrieved_evidence_str_escapes_xml_attributes():
    """XML special characters in authors and title are safely escaped."""
    chunks = [
        {
            "title": 'Paper with "Quotes" & <Brackets>',
            "authors": ["Smith & Sons", 'O"Connor'],
            "year": "2024",
            "chunk_type": "text",
            "text": "Test body.",
        }
    ]
    formatted = format_retrieved_evidence_str(chunks)
    assert 'authors="Smith &amp; Sons &amp; O&quot;Connor"' in formatted
    assert 'title="Paper with &quot;Quotes&quot; &amp; &lt;Brackets&gt;"' in formatted


# ---------------------------------------------------------------------------
# 5. Toolkit & Tools Metadata Propagation Tests
# ---------------------------------------------------------------------------


def test_search_corpus_core_extracts_authors_and_year():
    """search_corpus_core populates 'authors' and 'year' from DB item."""
    mock_syncer = MagicMock()
    mock_store = MagicMock()
    mock_db = MagicMock()
    mock_syncer.store = mock_store
    mock_syncer.db = mock_db

    fake_result = SearchResult(
        item_id=101,
        item_key="KEY_AUTH",
        chunk_index=1,
        chunk_text="Core transformers text.",
        heading_path=["Model"],
        score=0.04,
        raw_semantic_score=0.91,
        retrieval_method="hybrid",
        chunk_type="text",
        chunk_id="KEY_AUTH::1",
    )
    mock_store.search.return_value = [fake_result]

    mock_item = MagicMock()
    mock_item.title = "Attention Paper"
    mock_item.creators = ["Vaswani, Ashish", "Shazeer, Noam"]
    mock_item.year = "2017"
    mock_db.get_item.return_value = mock_item

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = search_corpus_core("attention", item_key="KEY_AUTH")

    assert res["status"] == "success"
    assert len(res["results"]) == 1
    r = res["results"][0]
    assert r["authors"] == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert r["year"] == "2017"


def test_read_document_core_passes_authors_and_year_from_toc(tmp_path: Path):
    """read_document_core passes authors and year from toc.json when available."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ITEM_META"
    bundle_dir.mkdir(parents=True)

    (bundle_dir / "document.md").write_text("# Section 1\nSome substantive content.\n", encoding="utf-8")
    toc_data = {
        "version": 1,
        "title": "Document With Meta",
        "authors": ["Author One", "Author Two"],
        "year": "2023",
        "total_lines": 2,
        "total_words": 4,
        "sections": [
            {"id": "s1", "heading": "Section 1", "start_line": 1, "end_line": 2, "word_count": 4}
        ],
        "figures": [],
        "tables": [],
    }
    (bundle_dir / "toc.json").write_text(json.dumps(toc_data), encoding="utf-8")

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    mock_syncer.db = None
    mock_syncer.store.get_intersecting_chunk_ids.return_value = ["ITEM_META::1"]

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        res = read_document_core("ITEM_META", asset_id="s1")

    assert res["status"] == "success"
    assert res["authors"] == ["Author One", "Author Two"]
    assert res["year"] == "2023"


def test_read_document_tool_forwards_authors_and_year_into_evidence():
    """read_document tool forwards authors and year into tool_context current_evidence."""
    mock_tc = MagicMock()
    mock_tc.state = {
        TEMP_SEEN_ASSETS: [],
        TEMP_SEEN_CHUNK_IDS: [],
        TEMP_RETRIEVED_EVIDENCE: [],
        ACTIVE_CONTEXT_ITEMS: [],
    }

    fake_core_res = {
        "status": "success",
        "item_key": "DOC123",
        "title": "Grounded Agents",
        "authors": ["Alice Smith", "Bob Jones", "Charlie Brown"],
        "year": "2025",
        "assets": [
            {
                "asset_id": "s1",
                "asset_type": "section",
                "heading": "Evaluation",
                "content": "Evaluation metrics and benchmarks.",
                "word_count": 5,
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
        res = read_document("DOC123", asset_id="s1", tool_context=mock_tc)
        assert res["status"] == "success"

        evidence = mock_tc.state[TEMP_RETRIEVED_EVIDENCE]
        assert len(evidence) == 1
        ev0 = evidence[0]
        assert ev0["title"] == "Grounded Agents"
        assert ev0["authors"] == ["Alice Smith", "Bob Jones", "Charlie Brown"]
        assert ev0["year"] == "2025"

        # When formatted, renders authors and year
        xml = format_retrieved_evidence_str(evidence)
        assert 'authors="Alice Smith et al."' in xml
        assert 'year="2025"' in xml
