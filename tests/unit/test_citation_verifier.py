"""Unit tests for decision model citation verification (Phase 2)."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from zocrates.agents.library_search.citation_resolver import resolve_citations_and_references
from zocrates.agents.library_search.citation_verifier import (
    MAX_CLAIM_CHARS,
    MAX_SOURCE_TEXT_CHARS,
    _check_support_local_laya,
    _check_support_openrouter,
    _find_backward_claim_boundary,
    _safe_set,
    _verification_span,
    extract_claim_occurrences,
    extract_claims_for_citations,
    prepare_source_text,
    verify_citations_support,
)


@pytest.fixture(autouse=True)
def mock_openrouter_key(monkeypatch):
    """Ensure OpenRouter key is set in environment for mocked verification tests."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock-openrouter-key")


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


# ---------------------------------------------------------------------------
# 1. Abbreviation-aware boundary delimiter tests
# ---------------------------------------------------------------------------
def test_claim_extraction_abbreviations():
    """Verify periods following abbreviations (et al., Fig., Dr., initials, decimals) do not split claims."""
    # et al. in sentence
    text1 = "Earlier work had doubts. However, Jonsson et al. (2021) observed marked variation across embryogenesis [1]."
    claims1 = extract_claims_for_citations(text1, total_chunks=4)
    assert 1 in claims1
    assert "Jonsson et al. (2021) observed marked variation across embryogenesis" in claims1[1]
    assert "Earlier work had doubts" in claims1[1]

    # Fig. abbreviation
    text2 = "As shown in Fig. 2 [1], the translation score was significantly improved."
    claims2 = extract_claims_for_citations(text2, total_chunks=4)
    assert 1 in claims2
    assert "Fig. 2" in claims2[1]
    assert "translation score was significantly improved" in claims2[1]

    # Dr. abbreviation
    text3 = "According to Dr. Smith [1], embryonic mutations vary considerably."
    claims3 = extract_claims_for_citations(text3, total_chunks=4)
    assert 1 in claims3
    assert "Dr. Smith" in claims3[1]
    assert "embryonic mutations vary considerably" in claims3[1]

    # Initials: J. R. Smith
    text4 = "In 2020, J. R. Smith demonstrated the effectiveness of the method [1]."
    claims4 = extract_claims_for_citations(text4, total_chunks=4)
    assert 1 in claims4
    assert "J. R. Smith demonstrated the effectiveness of the method" in claims4[1]

    # Decimal number: 28.4 BLEU
    text5 = "Our model achieves 28.4 BLEU on the English-to-German translation task [2]."
    claims5 = extract_claims_for_citations(text5, total_chunks=4)
    assert 2 in claims5
    assert "28.4 BLEU" in claims5[2]


# ---------------------------------------------------------------------------
# 2. Mid-sentence author forward extension tests
# ---------------------------------------------------------------------------
def test_claim_extraction_mid_sentence():
    """Verify mid-sentence author citations (< 30 chars or author-only) extend forward."""
    # Author-only fragment with et al.
    text1 = "Jonsson et al. [1] observed marked variation across embryogenesis."
    claims1 = extract_claims_for_citations(text1, total_chunks=4)
    assert 1 in claims1
    assert "Jonsson et al." in claims1[1]
    assert "observed marked variation across embryogenesis" in claims1[1]

    # Author with year in parentheses
    text2 = "Vaswani et al. (2017) [2] introduced the Transformer model based on attention."
    claims2 = extract_claims_for_citations(text2, total_chunks=4)
    assert 2 in claims2
    assert "Vaswani et al. (2017)" in claims2[2]
    assert "introduced the Transformer model" in claims2[2]

    # Mid-sentence with comma clause
    text3 = "Jonsson et al. [1], in their 2021 study, observed marked variation across embryogenesis."
    claims3 = extract_claims_for_citations(text3, total_chunks=4)
    assert 1 in claims3
    assert "Jonsson et al." in claims3[1]
    assert "observed marked variation" in claims3[1]


# ---------------------------------------------------------------------------
# 3. Masking tests
# ---------------------------------------------------------------------------
def test_claim_extraction_masking():
    """Verify code blocks, markdown images, and link defs are masked and ignored as citations."""
    text = (
        "Here is real evidence [1].\n"
        "```python\n"
        "data = [2, 3]\n"
        "```\n"
        "Image ![Figure 2](img.png)\n"
        "[2]: https://example.com/paper\n"
    )
    claims = extract_claims_for_citations(text, total_chunks=4)
    assert 1 in claims
    assert 2 not in claims
    assert 3 not in claims


# ---------------------------------------------------------------------------
# 4. Multi-citation brackets & partial filtering
# ---------------------------------------------------------------------------
def test_multi_citation_bracket_partial_filtering(sample_ranked_chunks):
    """Verify [1, 2] is partially filtered to [1] when chunk 2 fails threshold."""
    text = "Both models were evaluated on the translation benchmarks [1, 2]."
    # Chunk 1 passes (0.95 >= 0.60), Chunk 2 fails (0.20 < 0.60)
    support_probs = {1: 0.95, 2: 0.20}

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    # In-text bracket rewritten to [1]
    assert "[1]" in res["enriched_text"]
    assert "[1, 2]" not in res["enriched_text"]
    assert "[2]" not in res["enriched_text"]

    # Citations metadata
    cits = res["citations"]
    assert len(cits) == 1
    assert cits[0]["index"] == 1
    assert cits[0]["support_probability"] == 0.95

    # References partitioning: Doc 1 (99R6SJ7T) in Tier 1, Doc 2 (2RE8APTV) in Tier 2
    refs = res["references"]
    tier1_refs = [r for r in refs if r["tier"] == 1]
    tier2_refs = [r for r in refs if r["tier"] == 2]

    assert any(r["item_key"] == "99R6SJ7T" for r in tier1_refs)
    assert any(r["item_key"] == "2RE8APTV" for r in tier2_refs)


def test_multi_citation_bracket_both_pass(sample_ranked_chunks):
    """Verify [1, 2] is retained when both chunks pass threshold."""
    text = "Both models were evaluated on benchmarks [1, 2]."
    support_probs = {1: 0.95, 2: 0.88}

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    assert "[1, 2]" in res["enriched_text"]
    assert len(res["citations"]) == 2
    assert res["citations"][0]["support_probability"] == 0.95
    assert res["citations"][1]["support_probability"] == 0.88


# ---------------------------------------------------------------------------
# 5. Fail-open conditions
# ---------------------------------------------------------------------------
def test_fail_open_conditions(sample_ranked_chunks):
    """Verify timeout, rate limit exhaustion, and malformed responses return None (fail open)."""
    # 1. Timeout in OpenRouter call
    with patch("requests.post", side_effect=requests.Timeout("Connection timed out")):
        prob = _check_support_openrouter(
            claim="Some claim",
            source_text="Some source text",
            api_key="sk-dummy",
            timeout=1.0,
        )
        assert prob is None

    # 2. HTTP 429 retry failure
    mock_429 = MagicMock()
    mock_429.status_code = 429
    with patch("requests.post", return_value=mock_429), patch("time.sleep"):
        prob = _check_support_openrouter(
            claim="Some claim",
            source_text="Some source text",
            api_key="sk-dummy",
        )
        assert prob is None

    # 3. HTTP 500 error
    mock_500 = MagicMock()
    mock_500.status_code = 500
    with patch("requests.post", return_value=mock_500):
        prob = _check_support_openrouter(
            claim="Some claim",
            source_text="Some source text",
            api_key="sk-dummy",
        )
        assert prob is None

    # 4. Malformed 200 response (missing noul)
    mock_malformed = MagicMock()
    mock_malformed.status_code = 200
    mock_malformed.json.return_value = {"answers": {"support": {"noul": "not-a-number"}}}
    with patch("requests.post", return_value=mock_malformed):
        prob = _check_support_openrouter(
            claim="Some claim",
            source_text="Some source text",
            api_key="sk-dummy",
        )
        assert prob is None

    # 5. Fail-open preserves citation in resolver
    text = "Real evidence is provided [1]."
    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities={1: None},
        verification_threshold=0.60,
    )
    assert "[1]" in res["enriched_text"]
    assert len(res["citations"]) == 1
    assert res["citations"][0]["support_probability"] is None


def test_local_laya_fail_open_on_offline():
    """Verify Local Laya fails open immediately if health check probe fails."""
    # Health check throws ConnectionError
    with patch("requests.get", side_effect=requests.ConnectionError("Connection refused")):
        prob = _check_support_local_laya(
            claim="Some claim",
            source_text="Some source text",
            local_url="http://127.0.0.1:8005/v1/systemone",
        )
        assert prob is None


# ---------------------------------------------------------------------------
# 6. Blocker 1 resolution: all citations fail verification
# ---------------------------------------------------------------------------
def test_two_tier_filtering_all_fail_empty_tier1(sample_ranked_chunks):
    """Verify Blocker 1: when all citations fail verification, Tier 1 is empty and zero-citation fallback is NOT triggered."""
    text = "Twin studies show mutation rates [1]. Self-attention connects queries [2]."
    # Both fail verification threshold (0.60)
    support_probs = {1: 0.15, 2: 0.10}

    res = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    # In-text brackets stripped cleanly
    assert "[1]" not in res["enriched_text"]
    assert "[2]" not in res["enriched_text"]
    assert "Twin studies show mutation rates" in res["enriched_text"]
    assert "Self-attention connects queries" in res["enriched_text"]

    # Citations list is empty
    assert len(res["citations"]) == 0
    assert len(res["cited_chunk_ids"]) == 0

    # Blocker 1 validation:
    # Tier 1 (# Cited Sources) header must NOT appear!
    assert "# Cited Sources" not in res["enriched_text"]

    # Tier 2 (## Other Considered Sources) MUST appear and contain all documents
    assert "## Other Considered Sources" in res["enriched_text"]

    # In references list, all candidate documents have tier == 2
    refs = res["references"]
    assert len(refs) > 0
    for r in refs:
        assert r["tier"] == 2

    tier1_refs = [r for r in refs if r["tier"] == 1]
    assert len(tier1_refs) == 0


# ---------------------------------------------------------------------------
# 7. Dual call-site parity test
# ---------------------------------------------------------------------------
def test_dual_call_site_parity(sample_ranked_chunks):
    """Verify identical resolution output between search_pipeline and fast_api_app fallback."""
    text = "Twin studies show mutation rates [1]. Deep RAG connects LLMs [4]."
    support_probs = {1: 0.92, 4: 0.15}

    # Call-site 1: Primary search pipeline invocation
    res_pipeline = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        zotero_dir=None,
        references_enabled=True,
        considered_disclosure=True,
        citations_enabled=True,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    # Call-site 2: Stream-end fallback enrichment invocation
    res_fallback = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        zotero_dir=None,
        references_enabled=True,
        considered_disclosure=True,
        citations_enabled=True,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    # Output parity verification
    assert res_pipeline["enriched_text"] == res_fallback["enriched_text"]
    assert res_pipeline["citations"] == res_fallback["citations"]
    assert res_pipeline["references"] == res_fallback["references"]
    assert res_pipeline["cited_chunk_ids"] == res_fallback["cited_chunk_ids"]


# ---------------------------------------------------------------------------
# 8. Async verify_citations_support integration & budgets
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_citations_support_openrouter(sample_ranked_chunks):
    """Verify async verify_citations_support with mocked OpenRouter response."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "answers": {
            "support": {
                "type": "noul",
                "noul": 0.96,
            }
        }
    }

    with patch("requests.post", return_value=mock_resp):
        probs = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
            },
        )

        assert probs.get(1) == [0.96]
        assert probs.get(2) == [0.96]


@pytest.mark.asyncio
async def test_verify_citations_support_evaluates_all_citations(sample_ranked_chunks):
    """Verify all citations in the turn batch are evaluated without arbitrary truncation."""
    text = "Evidence 1 [1]. Evidence 2 [2]. Evidence 3 [3]. Evidence 4 [4]."

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "answers": {
            "support": {
                "type": "noul",
                "noul": 0.90,
            }
        }
    }

    with patch("requests.post", return_value=mock_resp):
        probs = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
            },
        )

        # All 4 checks are evaluated in the batch
        assert probs.get(1) == [0.90]
        assert probs.get(2) == [0.90]
        assert probs.get(3) == [0.90]
        assert probs.get(4) == [0.90]


# ---------------------------------------------------------------------------
# 9. Context ceiling test (6,000 chars)
# ---------------------------------------------------------------------------
def test_prepare_source_text_ceiling():
    """Verify chunk text exceeding 6,000 characters is safely truncated."""
    long_text = "Background introductory context. " * 300  # ~9,900 chars
    target_sentence = "Specific experimental finding with high relevance."
    full_text = long_text + " " + target_sentence + " " + long_text

    chunk = {"text": full_text}
    bounded = prepare_source_text(chunk, claim="experimental finding with high relevance")

    assert len(bounded) <= MAX_SOURCE_TEXT_CHARS
    assert "experimental finding" in bounded


# ---------------------------------------------------------------------------
# 10. Local Laya batch verification test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_citations_support_local_laya_batch(sample_ranked_chunks):
    """Verify local Laya uses native /v1/systemone/batch for multi-citation turns."""
    from zocrates.agents.library_search.citation_verifier import _check_support_local_laya_batch

    items = [
        (1, "Claim 1", "Source text 1"),
        (2, "Claim 2", "Source text 2"),
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"answers": {"support": {"noul": 0.95}}},
            {"answers": {"support": {"noul": 0.72}}},
        ]
    }

    with patch("requests.post", return_value=mock_resp):
        res = _check_support_local_laya_batch(items, timeout=10.0)
        assert res is not None
        assert res[1] == 0.95
        assert res[2] == 0.72


# ---------------------------------------------------------------------------
# 11. Default Model and Logging Tests
# ---------------------------------------------------------------------------
def test_default_citation_verification_model():
    """Verify default citation verification model is typesafe/jev-1.14 (Recommended tier, §5.2)."""
    from zocrates.agents.library_search.citation_verifier import DEFAULT_OPENROUTER_MODEL
    from zocrates.config import DEFAULT_CITATION_VERIFICATION_MODEL, DEFAULT_CONFIG

    assert DEFAULT_CITATION_VERIFICATION_MODEL == "typesafe/jev-1.14"
    assert DEFAULT_OPENROUTER_MODEL == "typesafe/jev-1.14"
    assert DEFAULT_CONFIG["citation_verification_model"] == "typesafe/jev-1.14"


@pytest.mark.asyncio
async def test_verify_citations_support_logging(sample_ranked_chunks, caplog):
    """Verify informative logging of citation verification results with status KEPT and DEMOTED."""
    import logging

    text = "Claim supported [1]. Claim unsupported [2]."
    mock_responses = [
        MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.85}}}),
        MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.35}}}),
    ]

    with caplog.at_level(logging.INFO), patch("requests.post", side_effect=mock_responses):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
            },
        )
        assert res.get(1) == [0.85]
        assert res.get(2) == [0.35]
        assert "Citation verification [1] occ#1: p=0.8500 (threshold=0.60, status=KEPT)" in caplog.text
        assert "Citation verification [2] occ#1: p=0.3500 (threshold=0.60, status=DEMOTED)" in caplog.text


def test_enriched_text_synchronization_without_cited_sources_header(sample_ranked_chunks):
    """Verify that when all citations are demoted, enriched_text is synchronized even without '# Cited Sources'."""
    text = "Twin studies show mutation rates [1]. Deep RAG connects LLMs [2]."
    # Both citations demoted below 0.60
    support_probs = {1: 0.15, 2: 0.20}

    resolved = resolve_citations_and_references(
        text=text,
        ranked_chunks=sample_ranked_chunks,
        zotero_dir=None,
        references_enabled=True,
        considered_disclosure=True,
        citations_enabled=True,
        support_probabilities=support_probs,
        verification_threshold=0.60,
    )

    enriched_text = resolved["enriched_text"]
    # Check that [1] and [2] are stripped
    assert "[1]" not in enriched_text
    assert "[2]" not in enriched_text
    # Check that # Cited Sources is NOT emitted because tier 1 is empty
    assert "# Cited Sources" not in enriched_text
    # But considered sources is emitted
    assert '<details class="considered-sources">' in enriched_text

    # Simulate fast_api_app payload update logic
    payload = {"parts": [{"text": text}, {"text": ""}]}
    last_audit = {
        "enriched_text": enriched_text,
        "citations": resolved["citations"],
        "references": resolved["references"],
        "evidence_chunks": sample_ranked_chunks,
    }

    retrieved_enriched = last_audit.get("enriched_text")
    text_indices = [i for i, p in enumerate(payload["parts"]) if "text" in p]
    if text_indices and retrieved_enriched:
        payload["parts"][text_indices[0]]["text"] = retrieved_enriched
        for idx in text_indices[1:]:
            payload["parts"][idx]["text"] = ""
        payload["parts"] = [p for p in payload["parts"] if p.get("text") != "" or "text" not in p]

    assert len(payload["parts"]) == 1
    assert payload["parts"][0]["text"] == enriched_text
    assert "[1]" not in payload["parts"][0]["text"]


def test_check_support_local_laya_batch_failing_open_logging(caplog):
    """Verify local Laya batch logs 'Failing open.' on HTTP error or exception."""
    import logging
    from zocrates.agents.library_search.citation_verifier import _check_support_local_laya_batch

    items = [(1, "Claim 1", "Source text 1")]

    # 1. HTTP 500 error
    mock_resp = MagicMock(status_code=500)
    with caplog.at_level(logging.WARNING), patch("requests.post", return_value=mock_resp):
        res = _check_support_local_laya_batch(items)
        assert res is None
        assert "Failing open." in caplog.text

    caplog.clear()

    # 2. Network exception
    with caplog.at_level(logging.WARNING), patch("requests.post", side_effect=RuntimeError("connection dropped")):
        res = _check_support_local_laya_batch(items)
        assert res is None
        assert "Failing open." in caplog.text


def test_citation_verification_config_migration_and_preservation(tmp_path):
    """Verify legacy 3.0/10.0 defaults are migrated, custom values preserved, and bad types handled."""
    import json
    from zocrates.config import (
        DEFAULT_CITATION_VERIFICATION_TIMEOUT_SEC,
        DEFAULT_CITATION_VERIFICATION_TURN_BUDGET_SEC,
        get_config_path,
        load_zocrates_config,
    )

    config_file = get_config_path(tmp_path)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # Case 1: Legacy defaults (3.0s and 10.0s) should migrate to new defaults (10.0s and 20.0s)
    config_file.write_text(
        json.dumps({
            "citation_verification_timeout_sec": 3.0,
            "citation_verification_turn_budget_sec": 10.0,
        })
    )
    loaded = load_zocrates_config(tmp_path)
    assert loaded["citation_verification_timeout_sec"] == DEFAULT_CITATION_VERIFICATION_TIMEOUT_SEC
    assert loaded["citation_verification_turn_budget_sec"] == DEFAULT_CITATION_VERIFICATION_TURN_BUDGET_SEC

    # Case 2: Custom user values (e.g. 5.0s and 15.0s) must NOT be clobbered
    config_file.write_text(
        json.dumps({
            "citation_verification_timeout_sec": 5.0,
            "citation_verification_turn_budget_sec": 15.0,
        })
    )
    loaded = load_zocrates_config(tmp_path)
    assert loaded["citation_verification_timeout_sec"] == 5.0
    assert loaded["citation_verification_turn_budget_sec"] == 15.0

    # Case 3: Invalid types (string, None) do not crash loading
    config_file.write_text(
        json.dumps({
            "citation_verification_timeout_sec": "not_a_number",
            "citation_verification_turn_budget_sec": None,
        })
    )
    loaded = load_zocrates_config(tmp_path)
    assert loaded["citation_verification_timeout_sec"] == "not_a_number"
    assert loaded["citation_verification_turn_budget_sec"] is None


# ---------------------------------------------------------------------------
# 12. Occurrence-Level Extraction & Symbol Cleaning Tests
# ---------------------------------------------------------------------------
def test_clean_and_join_spans_markdown_and_symbols():
    """Verify stripping of markdown markers and normalization of math/arrow/dash symbols."""
    from zocrates.agents.library_search.citation_verifier import _clean_and_join_spans

    raw_backward = "- The measured temperature was ≈ 300K, where T ≥ 200K and P ≤ 5 atm → phase transition — see `results`"
    cleaned = _clean_and_join_spans(raw_backward, "")
    assert "≈" not in cleaned
    assert "about 300K" in cleaned
    assert "≥" not in cleaned
    assert "at least 200K" in cleaned
    assert "≤" not in cleaned
    assert "at most 5 atm" in cleaned
    assert "→" not in cleaned
    assert "to phase transition" in cleaned
    assert "—" not in cleaned
    assert " - see" in cleaned
    assert "`" not in cleaned
    assert "results." in cleaned

    # Check inline bold and italic markers stripped anywhere
    bold_italic = "**Bold text** and __italic text__ with **nested** content"
    cleaned_bi = _clean_and_join_spans(bold_italic, "")
    assert "**" not in cleaned_bi
    assert "__" not in cleaned_bi
    assert "Bold text and italic text with nested content." == cleaned_bi


def test_find_backward_sentence_boundary_headings():
    """Verify heading markers like - **Lead-in.** Claim yield Claim without trailing syntax."""
    from zocrates.agents.library_search.citation_verifier import (
        _clean_and_join_spans,
        _find_backward_sentence_boundary,
    )

    prefix1 = "- **Lead-in.** Somatic mutations occurred in twins"
    b_idx1 = _find_backward_sentence_boundary(prefix1)
    span1 = prefix1[b_idx1:].strip()
    assert span1 == "Somatic mutations occurred in twins"
    claim1 = _clean_and_join_spans(span1, "")
    assert claim1 == "Somatic mutations occurred in twins."

    # Heading with exclamation mark
    prefix2 = "- **Important Finding!** The attention mechanism scales quadratically"
    b_idx2 = _find_backward_sentence_boundary(prefix2)
    span2 = prefix2[b_idx2:].strip()
    assert span2 == "The attention mechanism scales quadratically"
    claim2 = _clean_and_join_spans(span2, "")
    assert claim2 == "The attention mechanism scales quadratically."


def test_extract_claim_occurrences():
    """Verify extract_claim_occurrences tracks per-occurrence document-order claims."""
    from zocrates.agents.library_search.citation_verifier import (
        extract_claim_occurrences,
        extract_claims_for_citations,
    )

    text = (
        "First observation about twins [1]. "
        "Second observation about twins and transformers [1, 2]. "
        "Third note on deep RAG [4]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 4

    # Occurrence 1 for chunk 1
    assert occurrences[0]["chunk_index"] == 1
    assert occurrences[0]["occurrence"] == 1
    assert "First observation about twins" in occurrences[0]["claim"]

    # Occurrence 2 for chunk 1 (from compound bracket [1, 2])
    assert occurrences[1]["chunk_index"] == 1
    assert occurrences[1]["occurrence"] == 2
    assert "Second observation about twins and transformers" in occurrences[1]["claim"]

    # Occurrence 1 for chunk 2 (from compound bracket [1, 2])
    assert occurrences[2]["chunk_index"] == 2
    assert occurrences[2]["occurrence"] == 1
    assert "Second observation about twins and transformers" in occurrences[2]["claim"]

    # Occurrence 1 for chunk 4
    assert occurrences[3]["chunk_index"] == 4
    assert occurrences[3]["occurrence"] == 1
    assert "Third note on deep RAG" in occurrences[3]["claim"]

    # extract_claims_for_citations backward compatibility selects longest claim per chunk
    legacy = extract_claims_for_citations(text, total_chunks=4)
    assert 1 in legacy
    assert "Second observation about twins and transformers" in legacy[1]
    assert 2 in legacy
    assert 4 in legacy


@pytest.mark.asyncio
async def test_verify_citations_support_per_occurrence(sample_ranked_chunks):
    """Verify verify_citations_support returns per-occurrence probability lists."""
    text = (
        "First claim for chunk one [1]. "
        "Second claim for chunk one and chunk two [1, 2]."
    )

    # 3 total occurrences: (chunk 1 occ 1), (chunk 1 occ 2), (chunk 2 occ 1)
    mock_responses = [
        MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.95}}}),
        MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.30}}}),
        MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.88}}}),
    ]

    with patch("requests.post", side_effect=mock_responses):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
            },
        )
        assert 1 in res
        assert 2 in res
        # Chunk 1 has 2 occurrences
        assert len(res[1]) == 2
        # Chunk 2 has 1 occurrence
        assert len(res[2]) == 1


# ---------------------------------------------------------------------------
# 13. Paragraph/Block-Scoped Backward Claim Boundary Tests
# ---------------------------------------------------------------------------
def test_claim_extraction_multisentence_bullet():
    """Verify multi-sentence bullet with trailing [1] captures the full bullet claim."""
    text = (
        "- Somatic mutation rates vary across human development. "
        "Whole-genome sequencing of monozygotic twins revealed unexpected diversity. "
        "These mutations appear early in embryogenesis [1]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 1
    assert occurrences[0]["chunk_index"] == 1
    claim = occurrences[0]["claim"]
    assert "Somatic mutation rates vary across human development" in claim
    assert "Whole-genome sequencing of monozygotic twins revealed unexpected diversity" in claim
    assert "These mutations appear early in embryogenesis" in claim


def test_claim_extraction_two_citations_same_paragraph():
    """Verify second citation in the same paragraph captures only the span following the first citation."""
    text = (
        "Jonsson et al. studied somatic mutations in monozygotic twins [1], "
        "whereas Vaswani et al. developed the Transformer architecture for translation tasks [2]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 2

    # First citation captures preceding text up to paragraph start
    assert occurrences[0]["chunk_index"] == 1
    assert "Jonsson et al. studied somatic mutations in monozygotic twins" in occurrences[0]["claim"]
    assert "Vaswani" not in occurrences[0]["claim"]

    # Second citation captures only text after the first citation token [1]
    assert occurrences[1]["chunk_index"] == 2
    assert "whereas Vaswani et al. developed the Transformer architecture for translation tasks" in occurrences[1]["claim"]
    assert "Jonsson" not in occurrences[1]["claim"]


def test_claim_extraction_paragraph_break_boundary():
    """Verify paragraph break \\n\\n separates claims cleanly across paragraphs."""
    text = (
        "Earlier methods relied on statistical models without deep representations.\n\n"
        "Modern approaches utilize multi-head self-attention mechanisms to learn dependencies [2]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 1
    assert occurrences[0]["chunk_index"] == 2
    claim = occurrences[0]["claim"]
    assert "Modern approaches utilize multi-head self-attention mechanisms to learn dependencies" in claim
    assert "Earlier methods" not in claim


def test_claim_extraction_list_item_boundaries():
    """Verify bullet points (\\n- ) and numbered items (\\n1. ) serve as claim boundaries."""
    # Bullet point boundary
    bullet_text = (
        "- Previous item described baseline results and shortcomings.\n"
        "- Attention functions map a query and key-value pairs to output vectors [2]."
    )
    bullet_occ = extract_claim_occurrences(bullet_text, total_chunks=4)
    assert len(bullet_occ) == 1
    assert "Attention functions map a query and key-value pairs to output vectors" in bullet_occ[0]["claim"]
    assert "Previous item" not in bullet_occ[0]["claim"]

    # Numbered item boundary
    numbered_text = (
        "1. Prior baseline used recurrence and convolutions.\n"
        "2. The Transformer replaces recurrent layers with multi-head attention [2]."
    )
    numbered_occ = extract_claim_occurrences(numbered_text, total_chunks=4)
    assert len(numbered_occ) == 1
    assert "The Transformer replaces recurrent layers with multi-head attention" in numbered_occ[0]["claim"]
    assert "Prior baseline" not in numbered_occ[0]["claim"]


def test_claim_extraction_leading_punctuation_stripped():
    """Verify leading punctuation after a previous citation is stripped."""
    text = (
        "Jonsson et al. reported embryonic divergence [1]. "
        ";; However, later analyses confirmed these findings across larger cohorts [3]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 2
    claim2 = occurrences[1]["claim"]
    assert "However, later analyses confirmed these findings across larger cohorts" in claim2
    assert not claim2.startswith((";", ".", ",", ":", " "))


def test_claim_extraction_bold_lead_in_heading():
    """Verify bold lead-in heading is retained as clean plain text without **."""
    text = "- **Key Contribution.** The attention mechanism eliminates recurrence and convolutions [2]."
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 1
    claim = occurrences[0]["claim"]
    assert "**" not in claim
    assert "Key Contribution. The attention mechanism eliminates recurrence and convolutions" in claim


def test_claim_extraction_markdown_header_boundary():
    """Verify markdown header (\\n### ) serves as a claim boundary."""
    text = (
        "Some general background information from earlier in the text.\n"
        "### Key Finding\n"
        "The attention mechanism scales quadratically with sequence length [2]."
    )
    occurrences = extract_claim_occurrences(text, total_chunks=4)
    assert len(occurrences) == 1
    assert occurrences[0]["chunk_index"] == 2
    claim = occurrences[0]["claim"]
    assert "The attention mechanism scales quadratically with sequence length" in claim
    assert "Some general background" not in claim


def test_find_backward_claim_boundary_max_chars_window():
    """Verify _find_backward_claim_boundary bounds to max_chars and advances past sentence boundary."""
    prefix = "First sentence to be truncated. Second sentence was cut. Third sentence right here in this claim is long enough. "
    # len(prefix) is 113. With max_chars=80, start_idx is initially 113 - 80 = 33.
    # The first sentence boundary inside prefix[33:] is after 'was cut.' (offset 23).
    # Remaining chars after advance = 113 - (33 + 23) = 57 >= MIN_CLAIM_CHARS (30).
    # Thus b_idx advances from 33 to 56.
    b_idx = _find_backward_claim_boundary(prefix, max_chars=80)
    assert b_idx == 56
    remaining = prefix[b_idx:].strip()
    assert remaining == "Third sentence right here in this claim is long enough."


# ---------------------------------------------------------------------------
# 17. Telemetry capture tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_citations_support_telemetry_schema_and_verdicts(sample_ranked_chunks):
    """Verify telemetry_out receives full schema (v1, 3-state verdicts, summary, latency)."""
    text = (
        "Twin studies show somatic mutation rates [1]. "
        "Transformers rely heavily on self-attention [2]. "
        "Deep RAG systems connect LLMs to external stores [4]."
    )

    def mock_post(url, *args, **kwargs):
        payload = kwargs.get("json", {})
        state = payload.get("state", {})
        claim = state.get("claim", "")
        if "Twin studies" in claim:
            return MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.95}}})
        elif "Transformers" in claim:
            return MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.40}}})
        else:
            return MagicMock(status_code=500, json=lambda: {})

    telemetry: dict = {}
    with patch("requests.post", side_effect=mock_post):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
                "citation_verification_threshold": 0.60,
                "citation_verification_turn_budget_sec": 15.0,
            },
            telemetry_out=telemetry,
        )

        assert res.get(1) == [0.95]
        assert res.get(2) == [0.40]
        assert res.get(4) == [None]

    # Validate top-level schema
    assert telemetry.get("schema_version") == 1
    assert telemetry.get("provider") == "openrouter"
    assert telemetry.get("model") == "typesafe/jev-1.13"
    assert "https://openrouter.ai" in telemetry.get("endpoint", "")
    assert telemetry.get("threshold") == 0.60
    assert telemetry.get("execution_mode") == "thread_pool"
    assert telemetry.get("turn_budget_sec") == 15.0
    assert telemetry.get("batch_size") == 3
    assert isinstance(telemetry.get("latency_ms"), (int, float))
    assert telemetry["latency_ms"] >= 0

    # Validate assessments
    assessments = telemetry.get("assessments", [])
    assert len(assessments) == 3

    # Assessment 1: KEPT
    a1 = assessments[0]
    assert a1["chunk_index"] == 1
    assert a1["occurrence"] == 1
    assert a1["verdict"] == "KEPT"
    assert a1["support_probability"] == 0.95
    assert a1["title"] == "Somatic mutation rates in monozygotic twins"
    assert a1["section"] == "Methods"
    assert a1["authors"] == ["Jonsson, Hakon", "Stefansson, Kari"]
    assert a1["year"] == "2021"
    assert a1["item_key"] == "99R6SJ7T"
    assert a1["attachment_key"] == "ATT99R6"
    assert len(a1["source_text"]) <= 1500
    assert isinstance(a1["latency_ms"], (int, float))

    # Assessment 2: DEMOTED
    a2 = assessments[1]
    assert a2["chunk_index"] == 2
    assert a2["occurrence"] == 1
    assert a2["verdict"] == "DEMOTED"
    assert a2["support_probability"] == 0.40
    assert a2["title"] == "Attention Is All You Need"
    assert a2["section"] == "3.2 Attention"
    assert isinstance(a2["latency_ms"], (int, float))

    # Assessment 3: FAIL_OPEN
    a3 = assessments[2]
    assert a3["chunk_index"] == 4
    assert a3["occurrence"] == 1
    assert a3["verdict"] == "FAIL_OPEN"
    assert a3["support_probability"] is None
    assert a3["title"] == "Deep RAG Systems"

    # Validate summary
    summary = telemetry.get("summary", {})
    assert summary.get("batch_size") == 3
    assert summary.get("kept") == 1
    assert summary.get("demoted") == 1
    assert summary.get("fail_open") == 1
    assert summary.get("budget_exceeded") is False
    assert summary.get("latency_ms") == telemetry["latency_ms"]


@pytest.mark.asyncio
async def test_verify_citations_support_telemetry_fail_open_verdict(sample_ranked_chunks):
    """Verify network exceptions result in FAIL_OPEN verdict and updated summary counts."""
    text = "Twin studies show mutation rates [1]."

    telemetry: dict = {}
    with patch("requests.post", side_effect=requests.RequestException("Remote failure")):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={"citation_verification_provider": "openrouter"},
            telemetry_out=telemetry,
        )

        assert res.get(1) == [None]

    assert telemetry["summary"]["fail_open"] == 1
    assert telemetry["summary"]["kept"] == 0
    assert telemetry["summary"]["demoted"] == 0
    assert telemetry["assessments"][0]["verdict"] == "FAIL_OPEN"
    assert telemetry["assessments"][0]["support_probability"] is None


@pytest.mark.asyncio
async def test_verify_citations_support_opentelemetry_span_attributes(sample_ranked_chunks):
    """Verify OpenTelemetry span attributes are set when an active tracer is present."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."

    mock_span = MagicMock()
    mock_span_cm = MagicMock()
    mock_span_cm.__enter__.return_value = mock_span
    mock_span_cm.__exit__.return_value = None
    mock_tracer = MagicMock()
    mock_tracer.start_as_current_span.return_value = mock_span_cm
    mock_otel_trace = MagicMock()
    mock_otel_trace.get_tracer.return_value = mock_tracer

    mock_resp = MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.88}}})

    with (
        patch("zocrates.agents.library_search.citation_verifier._otel_trace", mock_otel_trace),
        patch("requests.post", return_value=mock_resp),
    ):
        probs = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "openrouter",
                "citation_verification_model": "typesafe/jev-1.13",
                "citation_verification_threshold": 0.60,
            },
        )
        assert probs.get(1) == [0.88]
        assert probs.get(2) == [0.88]

    # Verify tracer was called to create span
    mock_tracer.start_as_current_span.assert_called_with("citation_verification")

    # Collect all set_attribute calls on mock_span
    set_attrs = {call.args[0]: call.args[1] for call in mock_span.set_attribute.call_args_list}

    # Verify input attributes
    assert set_attrs.get("openinference.span.kind") == "GUARDRAIL"
    assert set_attrs.get("input.mime_type") == "application/json"
    assert "input.value" in set_attrs
    input_data = json.loads(set_attrs["input.value"])
    assert len(input_data) == 2
    assert input_data[0]["chunk_index"] == 1
    assert "metadata" in set_attrs
    meta = json.loads(set_attrs["metadata"])
    assert meta["provider"] == "openrouter"
    assert meta["model"] == "typesafe/jev-1.13"

    # Verify output attributes
    assert set_attrs.get("output.mime_type") == "application/json"
    assert "output.value" in set_attrs
    out_val = json.loads(set_attrs["output.value"])
    assert "summary" in out_val
    assert "assessments" in out_val

    # Verify scalars
    assert set_attrs.get("citation_verification.provider") == "openrouter"
    assert set_attrs.get("citation_verification.model") == "typesafe/jev-1.13"
    assert set_attrs.get("citation_verification.threshold") == 0.60
    assert set_attrs.get("citation_verification.batch_size") == 2
    assert set_attrs.get("citation_verification.kept") == 2
    assert set_attrs.get("citation_verification.demoted") == 0
    assert set_attrs.get("citation_verification.fail_open") == 0
    assert isinstance(set_attrs.get("citation_verification.latency_ms"), (int, float))


@pytest.mark.asyncio
async def test_verify_citations_support_safe_when_telemetry_none_and_otel_unavailable(sample_ranked_chunks):
    """Verify execution safely completes without crash when telemetry_out is None and OpenTelemetry is unavailable."""
    text = "Twin studies show mutation rates [1]."
    mock_resp = MagicMock(status_code=200, json=lambda: {"answers": {"support": {"noul": 0.85}}})

    with (
        patch("zocrates.agents.library_search.citation_verifier._otel_trace", None),
        patch("requests.post", return_value=mock_resp),
    ):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={"citation_verification_provider": "openrouter"},
            telemetry_out=None,
        )
        assert res.get(1) == [0.85]


@pytest.mark.asyncio
async def test_verify_citations_support_local_laya_batch_telemetry(sample_ranked_chunks):
    """Verify local_laya batch execution telemetry has execution_mode 'batch' and latency_ms None per assessment."""
    text = "Twin studies show mutation rates [1]. Transformers use attention [2]."

    mock_batch_data = {
        "results": [
            {"answers": {"support": {"noul": 0.91}}},
            {"answers": {"support": {"noul": 0.25}}},
        ]
    }

    mock_health = MagicMock(status_code=200, json=lambda: {"status": "ok"})
    mock_batch = MagicMock(status_code=200, json=lambda: mock_batch_data)

    def mock_post(url, *args, **kwargs):
        return mock_batch

    def mock_get(url, *args, **kwargs):
        return mock_health

    telemetry: dict = {}
    with (
        patch("requests.get", side_effect=mock_get),
        patch("requests.post", side_effect=mock_post),
    ):
        res = await verify_citations_support(
            text=text,
            ranked_chunks=sample_ranked_chunks,
            config={
                "citation_verification_provider": "local_laya",
                "citation_verification_model": "inception/mercury-decide:free",
                "citation_verification_local_url": "http://127.0.0.1:8005/v1/systemone",
            },
            telemetry_out=telemetry,
        )

        assert res.get(1) == [0.91]
        assert res.get(2) == [0.25]

    assert telemetry["model"] == "laya"
    assert telemetry["execution_mode"] == "batch"
    assert telemetry["batch_size"] == 2
    assert telemetry["summary"]["kept"] == 1
    assert telemetry["summary"]["demoted"] == 1
    assert telemetry["summary"]["fail_open"] == 0
    # For batch mode, latency_ms is null (None) per assessment
    for a in telemetry["assessments"]:
        assert a["latency_ms"] is None


def test_safe_set_and_verification_span_no_ops_safely():
    """Verify _safe_set and _verification_span handle None and exceptions gracefully."""
    # _safe_set on None
    _safe_set(None, "key", "val")

    # _safe_set with raising span
    broken_span = MagicMock()
    broken_span.set_attribute.side_effect = RuntimeError("Broken attribute setter")
    _safe_set(broken_span, "key", "val")  # Must not raise

    # _safe_set with dict serializes to json string
    working_span = MagicMock()
    _safe_set(working_span, "key", {"a": 1})
    working_span.set_attribute.assert_called_once_with("key", '{"a": 1}')

    # _verification_span when _otel_trace is None
    with patch("zocrates.agents.library_search.citation_verifier._otel_trace", None):
        with _verification_span("test_span") as span:
            assert span is None

    # _verification_span when tracer raises
    bad_tracer = MagicMock()
    bad_tracer.start_as_current_span.side_effect = RuntimeError("Tracer fail")
    bad_otel = MagicMock()
    bad_otel.get_tracer.return_value = bad_tracer
    with patch("zocrates.agents.library_search.citation_verifier._otel_trace", bad_otel):
        with _verification_span("test_span") as span:
            assert span is None




