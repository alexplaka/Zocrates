"""Unit tests for in-text citation and two-tier sources resolution."""

import statistics
from unittest.mock import patch
import pytest
from zocrates.agents.library_search.citation_resolver import (
    build_coordinator_view,
    resolve_citations_and_references,
)
from zocrates.agents.library_search.citation_verifier import verify_citations_support


@pytest.fixture
def sample_ranked_chunks():
    return [
        {
            "chunk_id": "JONSSON::1",
            "item_key": "99R6SJ7T",
            "attachment_key": "ATT99R6",
            "title": "Somatic mutation rates in monozygotic twins",
            "authors": ["Jonsson, Hakon", "Stefansson, Kari"],
            "year": "2021",
            "heading": "Methods",
            "text": "The somatic mutation rate was estimated using whole-genome sequencing of 387 pairs of monozygotic twins.",
            "chunk_type": "text",
        },
        {
            "chunk_id": "VASWANI::1",
            "item_key": "2RE8APTV",
            "attachment_key": "ATT2RE8",
            "title": "Attention Is All You Need",
            "authors": ["Vaswani", "Shazeer", "Parmar"],
            "year": "2017",
            "heading": "3.2 Attention",
            "text": "An attention function can be described as mapping a query and a set of key-value pairs to an output.",
            "chunk_type": "text",
        },
        {
            "chunk_id": "JONSSON::2",
            "item_key": "99R6SJ7T",
            "attachment_key": "ATT99R6",
            "title": "Somatic mutation rates in monozygotic twins",
            "authors": ["Jonsson, Hakon", "Stefansson, Kari"],
            "year": "2021",
            "heading": "Results",
            "text": "We identified a median of 5.2 early embryonic mutations per twin pair.",
            "chunk_type": "text",
        },
        {
            "chunk_id": "SMITH::1",
            "item_key": "VDKHGW8P",
            "attachment_key": "ATTVDKH",
            "title": "Deep RAG Systems",
            "authors": ["Smith, Jane"],
            "year": "2023",
            "heading": "Introduction",
            "text": "Retrieval augmented generation connects LLMs to external knowledge stores.",
            "chunk_type": "text",
        },
    ]


def test_regex_matching(sample_ranked_chunks):
    """Verify [1], [2], [1, 2], [1; 2], and fullwidth 【1】 are parsed correctly."""
    text = (
        "Twin studies show mutation rates [1]. "
        "Transformers use self-attention [2]. "
        "Both findings apply [1, 2] and [1; 2]. "
        "Alternative bracket notation 【1】 also resolves."
    )
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    # Citations extracted
    citation_indices = [c["index"] for c in res["citations"]]
    assert 1 in citation_indices
    assert 2 in citation_indices
    assert len(citation_indices) == 2

    # Cleaned text has standard bracket format
    assert "[1]" in res["enriched_text"]
    assert "[2]" in res["enriched_text"]
    assert "[1, 2]" in res["enriched_text"]
    assert "【1】" not in res["enriched_text"]  # Fullwidth normalized to [1]


def test_code_block_protection(sample_ranked_chunks):
    """Verify [1] inside fenced or inline code is not captured as a citation."""
    text = (
        "Here is real evidence [1].\n"
        "Consider this code snippet:\n"
        "```python\n"
        "arr = [1, 2, 3]\n"
        "val = arr[2]\n"
        "```\n"
        "And inline code `data[1]` should not be a citation."
    )
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    # Only chunk 1 should be captured
    citation_indices = [c["index"] for c in res["citations"]]
    assert citation_indices == [1]

    # Code blocks should remain intact
    assert "arr = [1, 2, 3]" in res["enriched_text"]
    assert "val = arr[2]" in res["enriched_text"]
    assert "`data[1]`" in res["enriched_text"]


def test_markdown_link_definition_protection(sample_ranked_chunks):
    """Verify markdown link definitions like [2]: http... are not treated as citation tokens."""
    text = (
        "Here is real evidence [1].\n"
        "[2]: https://example.com/paper\n"
        "And some other text.\n"
    )
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    # Only chunk 1 should be captured, chunk 2 should not be captured
    citation_indices = [c["index"] for c in res["citations"]]
    assert citation_indices == [1]

    # Link definition should remain intact in enriched_text
    assert "[2]: https://example.com/paper" in res["enriched_text"]


def test_out_of_bounds_index(sample_ranked_chunks):
    """Verify [99] is safely stripped without creating phantom references."""
    text = "Valid evidence [1] and hallucinated evidence [99] in text."
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    citation_indices = [c["index"] for c in res["citations"]]
    assert citation_indices == [1]
    assert "[1]" in res["enriched_text"]
    assert "[99]" not in res["enriched_text"]
    assert "hallucinated evidence in text" in res["enriched_text"]


def test_exact_section_and_quote_extraction(sample_ranked_chunks):
    """Verify chunk heading and text are mapped verbatim in citations."""
    text = "According to Jonsson et al. [1], methods were evaluated."
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    assert len(res["citations"]) == 1
    c1 = res["citations"][0]
    assert c1["index"] == 1
    assert c1["token"] == "[1]"
    assert c1["section"] == "Methods"
    assert "whole-genome sequencing of 387 pairs" in c1["quote"]
    assert c1["item_key"] == "99R6SJ7T"
    assert c1["authors"] == "Jonsson & Stefansson"
    assert c1["year"] == "2021"


def test_two_tier_partitioning_100_percent_recall(sample_ranked_chunks):
    """Verify U = C U R with zero dropped evidence across two tiers."""
    # Cite chunks 1 (Jonsson) and 2 (Vaswani). Smith (chunk 4) is un-cited.
    text = "Jonsson demonstrated rates [1] while Vaswani created transformers [2]."
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    all_keys = {c["item_key"] for c in sample_ranked_chunks}
    tier1 = [r for r in res["references"] if r["tier"] == 1]
    tier2 = [r for r in res["references"] if r["tier"] == 2]

    tier1_keys = {r["item_key"] for r in tier1}
    tier2_keys = {r["item_key"] for r in tier2}

    # Completeness: U = C U R
    assert tier1_keys | tier2_keys == all_keys
    # Disjointness: C intersect R = empty
    assert len(tier1_keys & tier2_keys) == 0

    assert "99R6SJ7T" in tier1_keys
    assert "2RE8APTV" in tier1_keys
    assert "VDKHGW8P" in tier2_keys

    # Markdown checks
    assert "# Cited Sources" in res["enriched_text"]
    assert "## Other Considered Sources" in res["enriched_text"]
    assert '<details class="considered-sources">' in res["enriched_text"]
    assert 'open="false"' not in res["enriched_text"]
    assert "1 additional source analyzed by search agent" in res["enriched_text"]
    assert 'data-item-key="99R6SJ7T"' in res["enriched_text"]
    assert 'data-item-key="VDKHGW8P"' in res["enriched_text"]


def test_zero_citations_fallback(sample_ranked_chunks):
    """Verify all evidence is retained under # Cited Sources if the LLM omits citations."""
    text = "This response discusses twins and attention mechanisms without any numeric citations."
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    # Citations list is empty
    assert len(res["citations"]) == 0

    # All documents promoted to Tier 1
    tier1 = [r for r in res["references"] if r["tier"] == 1]
    tier2 = [r for r in res["references"] if r["tier"] == 2]

    assert len(tier1) == 3  # 3 distinct item keys (Jonsson, Vaswani, Smith)
    assert len(tier2) == 0

    assert "# Cited Sources" in res["enriched_text"]
    assert "## Other Considered Sources" not in res["enriched_text"]
    assert 'data-item-key="99R6SJ7T"' in res["enriched_text"]
    assert 'data-item-key="2RE8APTV"' in res["enriched_text"]
    assert 'data-item-key="VDKHGW8P"' in res["enriched_text"]


def test_author_et_al_formatting():
    """Verify 1 author vs. 2+ authors formatting."""
    chunks = [
        {
            "chunk_id": "C1",
            "item_key": "K1",
            "title": "Single Author",
            "authors": ["Turing, Alan"],
            "year": "1950",
            "heading": "",
            "text": "Computing machinery.",
        },
        {
            "chunk_id": "C2",
            "item_key": "K2",
            "title": "Two Authors",
            "authors": ["Devlin, Jacob", "Chang, Ming-Wei"],
            "year": "2018",
            "heading": "",
            "text": "BERT pretraining.",
        },
        {
            "chunk_id": "C3",
            "item_key": "K3",
            "title": "Many Authors",
            "authors": ["He, Kaiming", "Zhang, Xiangyu", "Ren, Shaoqing", "Sun, Jian"],
            "year": "2016",
            "heading": "",
            "text": "ResNet architecture.",
        },
    ]

    res = resolve_citations_and_references("Claims [1], [2], [3].", chunks)
    c1 = next(c for c in res["citations"] if c["index"] == 1)
    c2 = next(c for c in res["citations"] if c["index"] == 2)
    c3 = next(c for c in res["citations"] if c["index"] == 3)

    assert c1["authors"] == "Turing"
    assert c2["authors"] == "Devlin & Chang"
    assert c3["authors"] == "He et al."


def test_cited_chunk_ids_population(sample_ranked_chunks):
    """Verify chunk IDs are tracked for the audit record."""
    text = "We cite chunk 1 [1] and chunk 3 [3]."
    res = resolve_citations_and_references(text, sample_ranked_chunks)

    # Chunk 1: JONSSON::1, Chunk 3: JONSSON::2
    assert res["cited_chunk_ids"] == ["JONSSON::1", "JONSSON::2"]


def test_references_disabled_flag(sample_ranked_chunks):
    """Verify references section is omitted when references_enabled is False."""
    text = "Claim supported by evidence [1]."
    res = resolve_citations_and_references(
        text, sample_ranked_chunks, references_enabled=False
    )
    assert "# Cited Sources" not in res["enriched_text"]
    assert len(res["citations"]) == 1


def test_get_synthesizer_instruction():
    """Verify synthesizer prompt toggles between numeric bracket requirements and standard natural attribution."""
    from zocrates.agents.library_search.search_pipeline import get_synthesizer_instruction

    inst_enabled = get_synthesizer_instruction(citations_enabled=True)
    assert "[1]" in inst_enabled
    assert "numeric citation token" in inst_enabled

    inst_disabled = get_synthesizer_instruction(citations_enabled=False)
    assert "Do NOT invent artificial numeric citations" in inst_disabled
    assert "numeric citation token" not in inst_disabled


def test_extractive_quote_preserves_full_supporting_text():
    """Verify that chunk text is preserved in full without artificial 1000-char truncation."""
    long_text = "A" * 1500
    chunk = {
        "chunk_id": "TEST::1",
        "item_key": "KEY1",
        "title": "Long Paper",
        "authors": ["Author"],
        "year": "2024",
        "heading": "Long Section",
        "text": long_text,
    }
    res = resolve_citations_and_references("Claim [1]", [chunk])
    assert len(res["citations"]) == 1
    quote = res["citations"][0]["quote"]
    assert quote == long_text
    assert len(quote) == 1500


def test_extractive_quote_honors_explicit_max_chars(monkeypatch):
    """Verify that if MAX_EXTRACTIVE_QUOTE_CHARS is explicitly configured, it truncates."""
    from zocrates.agents.library_search import citation_resolver

    monkeypatch.setattr(citation_resolver, "MAX_EXTRACTIVE_QUOTE_CHARS", 500)
    long_text = "B" * 800
    chunk = {
        "chunk_id": "TEST::2",
        "item_key": "KEY2",
        "title": "Capped Paper",
        "authors": ["Author"],
        "year": "2024",
        "heading": "Capped Section",
        "text": long_text,
    }
    res = resolve_citations_and_references("Claim [1]", [chunk])
    assert len(res["citations"]) == 1
    quote = res["citations"][0]["quote"]
    assert len(quote) == 503  # 500 + "..."
    assert quote.endswith("...")
    assert quote.startswith("B" * 500)


def test_build_coordinator_view_single_citation():
    """Verify single in-text citation tokens ([1], [Chunk 1], [E 1], 【1】) are substituted with (Author, Year)."""
    citations = [
        {
            "index": 1,
            "authors": "Watson & Crick",
            "year": "1953",
            "title": "Molecular Structure of Nucleic Acids",
        }
    ]
    text = (
        "Standard citation [1] followed by chunk citation [Chunk 1] "
        "and evidence citation [E 1] and fullwidth 【1】."
    )
    result = build_coordinator_view(text, citations, [])
    assert "[1]" not in result
    assert "[Chunk 1]" not in result
    assert "[E 1]" not in result
    assert "【1】" not in result
    assert "(Watson & Crick, 1953)" in result
    # Punctuation cleanliness check
    assert result.endswith("(Watson & Crick, 1953).")


def test_build_coordinator_view_multi_citation():
    """Verify multi-citations ([1, 2], [1; 2]) format as (Author1, Year1; Author2, Year2) with deduplication."""
    citations = [
        {"index": 1, "authors": "Watson & Crick", "year": "1953"},
        {"index": 2, "authors": "Franklin", "year": "1952"},
        {"index": 3, "authors": "Watson & Crick", "year": "1953"},  # Same paper second chunk
    ]
    text = "Combined evidence [1, 2] and semicolon notation [1; 2] and same paper chunks [1, 3]."
    result = build_coordinator_view(text, citations, [])
    assert "(Watson & Crick, 1953; Franklin, 1952)" in result
    assert "[1, 2]" not in result
    assert "[1; 2]" not in result
    # Chunk 1 and Chunk 3 should deduplicate within the parenthetical
    assert "same paper chunks (Watson & Crick, 1953)." in result


def test_build_coordinator_view_code_block_masking():
    """Verify code brackets (e.g. arr[1], matrix[1, 2]) are protected inside fenced and inline code blocks."""
    citations = [{"index": 1, "authors": "Knuth", "year": "1984"}]
    text = (
        "Here is a code example:\n"
        "```python\n"
        "arr = [1, 2, 3]\n"
        "val = arr[1]\n"
        "```\n"
        "And inline code `data[1]` should stay as code.\n"
        "While real evidence [1] is cited."
    )
    result = build_coordinator_view(text, citations, [])
    assert "val = arr[1]" in result
    assert "`data[1]`" in result
    assert "arr = [1, 2, 3]" in result
    assert "real evidence (Knuth, 1984) is cited." in result


def test_build_coordinator_view_clean_markdown_bibliography():
    """Verify bibliography generates clean Markdown without HTML tags, details/summary, or internal item keys."""
    raw_response_with_html = (
        "Recent research demonstrates significant findings [1]. "
        "Alternative approaches were also explored [2].\n\n"
        "# Cited Sources\n"
        '- <span class="zotero-source-link" data-item-key="ITEMKEY1">**Jonsson & Stefansson (2021)**. *Somatic mutation rates*. Nature.</span>\n\n'
        "## Other Considered Sources\n"
        '<details class="considered-sources">\n'
        "  <summary>1 additional source analyzed by search agent</summary>\n"
        "  <ul>\n"
        '    <li><span class="zotero-source-link" data-item-key="ITEMKEY2"><strong>Smith (2023)</strong>. <em>Deep RAG</em>.</span></li>\n'
        "  </ul>\n"
        "</details>"
    )
    citations = [
        {"index": 1, "authors": "Jonsson & Stefansson", "year": "2021", "title": "Somatic mutation rates"},
        {"index": 2, "authors": "Vaswani et al.", "year": "2017", "title": "Attention Is All You Need"},
    ]
    references = [
        {
            "tier": 1,
            "item_key": "ITEMKEY1",
            "authors": ["Jonsson, Hakon", "Stefansson, Kari"],
            "year": "2021",
            "title": "Somatic mutation rates in monozygotic twins",
            "publication": "Nature",
        },
        {
            "tier": 1,
            "item_key": "ITEMKEY3",
            "authors": "Vaswani et al.",
            "year": 2017,
            "title": "Attention Is All You Need",
            "publication": "NeurIPS",
        },
        {
            "tier": 2,
            "item_key": "ITEMKEY2",
            "authors": ["Smith, Jane"],
            "year": "2023",
            "title": "Deep RAG Systems",
            "publication": "",
        },
    ]

    result = build_coordinator_view(raw_response_with_html, citations, references)

    # In-text parenthetical citations substituted
    assert "(Jonsson & Stefansson, 2021)" in result
    assert "(Vaswani et al., 2017)" in result
    assert "[1]" not in result
    assert "[2]" not in result

    # Bibliography headers
    assert "# Cited Sources" in result
    assert "## Other Considered Sources" in result

    # Clean Markdown entries
    assert "- **Jonsson & Stefansson (2021)**. *Somatic mutation rates in monozygotic twins*. Nature." in result
    assert "- **Vaswani et al. (2017)**. *Attention Is All You Need*. NeurIPS." in result
    assert "- **Smith (2023)**. *Deep RAG Systems*." in result

    # Zero HTML tags
    assert "<span" not in result
    assert "</span>" not in result
    assert "<details" not in result
    assert "</details>" not in result
    assert "<summary" not in result
    assert "</summary>" not in result
    assert "<ul" not in result
    assert "<li" not in result
    assert "<strong" not in result
    assert "<em" not in result

    # Zero item keys
    assert "data-item-key" not in result
    assert "ITEMKEY1" not in result
    assert "ITEMKEY2" not in result
    assert "ITEMKEY3" not in result


def test_build_coordinator_view_empty_and_missing_metadata():
    """Verify graceful handling of empty inputs, missing metadata, and out-of-bounds citations."""
    # Empty inputs
    assert build_coordinator_view("", [], []) == ""
    assert build_coordinator_view(None, None, None) == ""

    # Missing metadata in citations
    citations = [
        {"index": 1, "authors": "Only Author"},
        {"index": 2, "year": "1999"},
        {"index": 3, "title": "Only Title"},
        {"index": 4},  # Empty metadata
    ]
    text = "Author citation [1], year citation [2], title citation [3], empty citation [4], hallucinated citation [99]."
    result = build_coordinator_view(text, citations, [])
    assert "(Only Author)" in result
    assert "(1999)" in result
    assert "(Only Title)" in result
    # Empty metadata and hallucinated [99] stripped, whitespace cleaned before period
    assert "[4]" not in result
    assert "[99]" not in result
    assert result.endswith("hallucinated citation.")

    # Missing metadata in references
    references = [
        {"tier": 1, "title": "Paper Without Author Or Year"},
        {"tier": 1, "authors": "Author Only", "title": "Paper With Author Only"},
        {"tier": 1, "year": "2024", "title": "Paper With Year Only"},
    ]
    ref_result = build_coordinator_view("Prose.", [], references)
    assert "- *Paper Without Author Or Year*." in ref_result
    assert "- **Author Only**. *Paper With Author Only*." in ref_result
    assert "- **(2024)**. *Paper With Year Only*." in ref_result


def test_build_coordinator_view_edge_cases():
    """Verify mid-sentence dropped citation space collapse, fullwidth multi-citations, and unclosed brackets."""
    citations = [
        {"index": 1, "authors": "Watson & Crick", "year": "1953"},
        {"index": 2, "authors": "Franklin", "year": "1952"},
    ]
    # 1. Mid-sentence dropped token collapses double space to single space
    text_dropped = "Some evidence [99] presented here."
    result_dropped = build_coordinator_view(text_dropped, citations, [])
    assert result_dropped == "Some evidence presented here."

    # 2. Fullwidth multi-citations
    text_fullwidth = "Comparative results 【1, 2】 show agreement."
    result_fullwidth = build_coordinator_view(text_fullwidth, citations, [])
    assert "Comparative results (Watson & Crick, 1953; Franklin, 1952) show agreement." in result_fullwidth

    # 3. Unclosed bracket stays untouched
    text_unclosed = "Index notation arr[1 is unclosed."
    result_unclosed = build_coordinator_view(text_unclosed, citations, [])
    assert result_unclosed == "Index notation arr[1 is unclosed."


def test_config_permutations_citations_off_references_on(sample_ranked_chunks):
    """citations_enabled=False, references_enabled=True: tokens stripped, # Cited Sources present via fallback."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."
    res = resolve_citations_and_references(
        text,
        sample_ranked_chunks,
        citations_enabled=False,
        references_enabled=True,
    )
    # In-text citation tokens must be stripped
    assert "[1]" not in res["enriched_text"]
    assert "[2]" not in res["enriched_text"]
    # No citation metadata (tokens were stripped, not parsed)
    assert res["citations"] == []
    assert res["cited_chunk_ids"] == []
    # Zero-citation fallback: all docs promoted to Tier 1
    tier1 = [r for r in res["references"] if r["tier"] == 1]
    tier2 = [r for r in res["references"] if r["tier"] == 2]
    assert len(tier1) == 3  # 3 distinct item keys (Jonsson, Vaswani, Smith)
    assert len(tier2) == 0
    # References section present
    assert "# Cited Sources" in res["enriched_text"]
    assert 'data-item-key="99R6SJ7T"' in res["enriched_text"]
    assert 'data-item-key="2RE8APTV"' in res["enriched_text"]
    assert 'data-item-key="VDKHGW8P"' in res["enriched_text"]


def test_config_permutations_citations_off_references_off(sample_ranked_chunks):
    """citations_enabled=False, references_enabled=False: tokens stripped, no section, references=[]."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."
    res = resolve_citations_and_references(
        text,
        sample_ranked_chunks,
        citations_enabled=False,
        references_enabled=False,
    )
    # In-text citation tokens must be stripped
    assert "[1]" not in res["enriched_text"]
    assert "[2]" not in res["enriched_text"]
    # No citation or reference metadata
    assert res["citations"] == []
    assert res["cited_chunk_ids"] == []
    assert res["references"] == []
    # No references section
    assert "# Cited Sources" not in res["enriched_text"]
    assert "## Other Considered Sources" not in res["enriched_text"]


def test_config_permutations_citations_on_references_off(sample_ranked_chunks):
    """citations_enabled=True, references_enabled=False: tokens parsed, citations populated, no section, references=[]."""
    text = "Jonsson demonstrated rates [1] while Vaswani created transformers [2]."
    res = resolve_citations_and_references(
        text,
        sample_ranked_chunks,
        citations_enabled=True,
        references_enabled=False,
    )
    # In-text tokens preserved
    assert "[1]" in res["enriched_text"]
    assert "[2]" in res["enriched_text"]
    # Citations populated
    citation_indices = [c["index"] for c in res["citations"]]
    assert 1 in citation_indices
    assert 2 in citation_indices
    # References section suppressed; references list empty
    assert res["references"] == []
    assert "# Cited Sources" not in res["enriched_text"]
    assert "## Other Considered Sources" not in res["enriched_text"]


def test_config_permutations_citations_on_references_on(sample_ranked_chunks):
    """citations_enabled=True, references_enabled=True: default behavior, both populated."""
    text = "Jonsson demonstrated rates [1] while Vaswani created transformers [2]."
    res = resolve_citations_and_references(
        text,
        sample_ranked_chunks,
        citations_enabled=True,
        references_enabled=True,
    )
    # In-text tokens preserved
    assert "[1]" in res["enriched_text"]
    assert "[2]" in res["enriched_text"]
    # Citations populated
    citation_indices = [c["index"] for c in res["citations"]]
    assert 1 in citation_indices
    assert 2 in citation_indices
    # References section present with both tiers
    assert "# Cited Sources" in res["enriched_text"]
    tier1 = [r for r in res["references"] if r["tier"] == 1]
    tier2 = [r for r in res["references"] if r["tier"] == 2]
    assert len(tier1) == 2  # Jonsson and Vaswani cited
    assert len(tier2) == 1  # Smith uncited


# ---------------------------------------------------------------------------
# Per-occurrence citation verification & compound bracket tests
# ---------------------------------------------------------------------------
def test_resolve_citations_per_occurrence_partial_filtering(sample_ranked_chunks):
    """Verify per-occurrence probabilities: occurrence 1 survives, occurrence 2 stripped."""
    text = (
        "First supported statement about somatic rates [1]. "
        "Second unsupported statement about somatic rates [1]. "
        "Third supported statement about transformers [2]."
    )
    # Chunk 1: occ 1 = 0.95 (survives), occ 2 = 0.25 (demoted)
    # Chunk 2: occ 1 = 0.88 (survives)
    support_probs = {
        1: [0.95, 0.25],
        2: [0.88],
    }

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    enriched = res["enriched_text"]
    # First [1] kept
    assert "First supported statement about somatic rates [1]." in enriched
    # Second [1] stripped without double space or punctuation gap
    assert "Second unsupported statement about somatic rates." in enriched
    # [2] kept
    assert "Third supported statement about transformers [2]." in enriched

    # Citations metadata
    cits = res["citations"]
    c1 = next(c for c in cits if c["index"] == 1)
    assert c1["support_probability"] == 0.95
    assert len(c1["occurrences"]) == 2
    assert c1["occurrences"][0] == {"occurrence": 1, "support_probability": 0.95, "survived": True}
    assert c1["occurrences"][1] == {"occurrence": 2, "support_probability": 0.25, "survived": False}

    c2 = next(c for c in cits if c["index"] == 2)
    assert c2["support_probability"] == 0.88
    assert len(c2["occurrences"]) == 1
    assert c2["occurrences"][0] == {"occurrence": 1, "support_probability": 0.88, "survived": True}

    # Tier 1 partitioning: Doc 1 (99R6SJ7T) and Doc 2 (2RE8APTV) both have >= 1 surviving occurrence
    tier1_keys = [r["item_key"] for r in res["references"] if r["tier"] == 1]
    assert "99R6SJ7T" in tier1_keys
    assert "2RE8APTV" in tier1_keys


def test_resolve_citations_compound_bracket_per_occurrence(sample_ranked_chunks):
    """Verify compound bracket [1, 2] is partially pruned to [1] when chunk 2 fails."""
    text = (
        "Combined analysis shows significant gains [1, 2]. "
        "Unsupported follow-up claims [1, 2]."
    )
    # In first bracket [1, 2]: chunk 1 (occ 1) passes, chunk 2 (occ 1) fails -> becomes [1]
    # In second bracket [1, 2]: chunk 1 (occ 2) fails, chunk 2 (occ 2) passes -> becomes [2]
    support_probs = {
        1: [0.92, 0.20],
        2: [0.35, 0.85],
    }

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    enriched = res["enriched_text"]
    assert "Combined analysis shows significant gains [1]." in enriched
    assert "Unsupported follow-up claims [2]." in enriched
    assert "[1, 2]" not in enriched


def test_resolve_citations_legacy_flat_dict_normalization(sample_ranked_chunks):
    """Verify legacy flat dict support_probabilities is normalized across occurrences."""
    text = (
        "First statement [1]. "
        "Second statement [1, 2]. "
        "Third statement [2]."
    )
    # Flat dict: chunk 1 passes everywhere, chunk 2 fails everywhere
    support_probs = {1: 0.95, 2: 0.30}

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    enriched = res["enriched_text"]
    assert "First statement [1]." in enriched
    assert "Second statement [1]." in enriched
    assert "Third statement." in enriched
    assert "[2]" not in enriched


@pytest.mark.asyncio
async def test_verify_citations_support_stats_computation(sample_ranked_chunks):
    """Verify mean_support and std_support calculation across multiple claims."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."
    telemetry = {}

    call_count = 0

    def mock_check_support(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return 0.90 if call_count == 1 else 0.70

    with patch("zocrates.agents.library_search.citation_verifier._check_support_openrouter", side_effect=mock_check_support):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={"citation_verification_provider": "openrouter", "citation_verification_threshold": 0.60},
            telemetry_out=telemetry,
        )

    assert telemetry["summary"]["batch_size"] == 2
    assert telemetry["summary"]["kept"] == 2
    assert telemetry["summary"]["demoted"] == 0
    assert telemetry["summary"]["mean_support"] == 0.800
    expected_std = round(statistics.stdev([0.90, 0.70]), 3)
    assert telemetry["summary"]["std_support"] == expected_std

    # Ensure every assessment contains all required fields
    assessments = telemetry["assessments"]
    assert len(assessments) == 2
    for a in assessments:
        assert "claim" in a
        assert "source_text" in a
        assert "title" in a
        assert "authors" in a
        assert "year" in a
        assert "section" in a
        assert "support_probability" in a
        assert "threshold" in a
        assert a["threshold"] == 0.60
        assert "verdict" in a
        assert a["verdict"] == "KEPT"


@pytest.mark.asyncio
async def test_verify_citations_support_stats_single_and_fail_open(sample_ranked_chunks):
    """Verify std_support is 0.0 when N=1, and None when all fail open."""
    text = "Twin studies show mutation rates [1]."
    telemetry = {}
    config = {
        "citation_verification_provider": "openrouter",
        "citation_verification_model": "inception/mercury-decide:free",
        "citation_verification_threshold": 0.60,
    }

    with patch("zocrates.agents.library_search.citation_verifier._check_support_openrouter", return_value=0.85):
        await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config=config,
            telemetry_out=telemetry,
        )

    assert telemetry["summary"]["batch_size"] == 1
    assert telemetry["summary"]["mean_support"] == 0.850
    assert telemetry["summary"]["std_support"] == 0.0

    # Fail open (None probability)
    telemetry_fail = {}
    with patch("zocrates.agents.library_search.citation_verifier._check_support_openrouter", return_value=None):
        await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config=config,
            telemetry_out=telemetry_fail,
        )

    assert telemetry_fail["summary"]["fail_open"] == 1
    assert telemetry_fail["summary"]["mean_support"] is None
    assert telemetry_fail["summary"]["std_support"] is None
    assert telemetry_fail["assessments"][0]["verdict"] == "FAIL_OPEN"
    assert telemetry_fail["assessments"][0]["threshold"] == 0.60


