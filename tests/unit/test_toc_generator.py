"""
Unit tests for ToC Generator, Summary Generator, and DocumentProcessor ToC augmentation.
"""

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.rag.document_processor import DocumentProcessor, _open_meta_db
from zocrates.rag.summary_generator import extract_lead_text, generate_summary
from zocrates.rag.toc_generator import generate_toc, save_toc_files


# ---------------------------------------------------------------------------
# 1. Summary Generator & Extractive Lead Text Tests
# ---------------------------------------------------------------------------


def test_extract_lead_text_in_body_abstract():
    sample_doc = """---
type: document
title: Attention Is All You Need
---

# Attention Is All You Need

## Abstract
The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder. The best performing models also connect the encoder and decoder through an attention mechanism. We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.

## 1 Introduction
Recurrent neural networks have been firmly established as state of the art.
"""
    extracted = extract_lead_text(sample_doc)
    assert "dominant sequence transduction models" in extracted
    assert "based solely on attention mechanisms" in extracted
    assert not extracted.startswith("## Abstract")


def test_extract_lead_text_prose_accumulation_with_noise():
    sample_doc = """# Technical Report

```python
# This is a code comment
def main():
    pass
```

Department of Computer Science, Stanford University.
arXiv:2301.12345v1 [cs.AI] 25 Jan 2023.

<!-- Internal build note -->
<figure data-type="image">
![Logo](images/logo.png)
</figure>

Deep neural networks have achieved remarkable empirical success across a diverse range of challenging perceptual and reasoning domains. However, standard architectures often struggle to maintain consistency across long reasoning trajectories. In this work, we investigate mechanisms for verifiable multistep deduction in large language models.

Furthermore, empirical evaluations across eight standard benchmarks indicate that structured chain-of-thought verification improves accuracy by 14.2% over existing baselines.
"""
    extracted = extract_lead_text(sample_doc)
    assert "Deep neural networks have achieved remarkable" in extracted
    assert "verifiable multistep deduction" in extracted
    assert "Stanford University" not in extracted
    assert "arXiv:" not in extracted
    assert "def main():" not in extracted


def test_extract_lead_text_fallback_empty():
    sample_doc = """```python
print("Hello World")
```
"""
    extracted = extract_lead_text(sample_doc)
    assert extracted == "No abstract or summary available."


def test_generate_summary_openrouter_success():
    sample_text = "This paper presents a novel distributed architecture for high-throughput stream processing."

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "This paper introduces a distributed streaming architecture achieving 10x throughput improvements over prior baselines."
    mock_resp.choices = [mock_choice]

    with patch("zocrates.rag.summary_generator.litellm.completion", return_value=mock_resp) as mock_comp:
        with patch("zocrates.rag.summary_generator.get_secure_api_key", return_value="sk-or-testkey"):
            with patch("zocrates.rag.summary_generator.load_zocrates_config", return_value={}):
                summary, source = generate_summary(sample_text, title="Distributed Streaming")
                assert "distributed streaming architecture" in summary
                assert source == "generated"
                assert mock_comp.call_args[1]["model"] == "openrouter/nvidia/nemotron-3.5-lightning:free"
                assert mock_comp.call_args[1]["extra_body"] == {"reasoning": {"effort": "none"}}


def test_generate_summary_model_override():
    sample_text = "This paper presents a novel distributed architecture for high-throughput stream processing."

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "This paper introduces a distributed streaming architecture achieving 10x throughput improvements over prior baselines."
    mock_resp.choices = [mock_choice]

    with patch("zocrates.rag.summary_generator.litellm.completion", return_value=mock_resp) as mock_comp:
        with patch("zocrates.rag.summary_generator.get_secure_api_key", return_value="sk-or-testkey"):
            summary, source = generate_summary(
                sample_text,
                title="Distributed Streaming",
                model="openai/gpt-4o-mini",
            )
            assert source == "generated"
            assert mock_comp.call_args[1]["model"] == "openrouter/openai/gpt-4o-mini"


def test_generate_summary_openrouter_failure_falls_back_to_extractive():
    sample_text = """## Abstract
This study investigates quantum error correction in topological surface codes under realistic noise models. We present a scalable decoding algorithm that reduces logical error rates by an order of magnitude.
"""

    with patch("zocrates.rag.summary_generator.litellm.completion", side_effect=Exception("API rate limit exceeded 429")):
        with patch("zocrates.rag.summary_generator.get_secure_api_key", return_value="sk-or-testkey"):
            summary, source = generate_summary(sample_text, title="Quantum Error Correction")
            assert "quantum error correction" in summary.lower()
            assert source == "extractive"


def test_generate_summary_no_api_key_falls_back_to_extractive_without_calling_llm():
    """Verify that generate_summary falls back to extractive lead text without attempting LLM when no key exists."""
    sample_text = """## Abstract
Deterministic extractive lead text is used directly when no OpenRouter API key is configured. We present a scalable approach for offline summarization without network requests or external dependencies.
"""
    with patch("zocrates.rag.summary_generator.litellm.completion") as mock_comp:
        with patch("zocrates.rag.summary_generator.get_secure_api_key", return_value=None):
            with patch("zocrates.rag.summary_generator.load_zocrates_config", return_value={"llm_provider": "openrouter_llm"}):
                summary, source = generate_summary(sample_text, title="Deterministic Fallback")
                assert source == "extractive"
                assert "Deterministic extractive lead text" in summary
                mock_comp.assert_not_called()


def test_generate_summary_strips_thinking_tags():
    """Verify that closed and unclosed <think> tags are stripped from generated summaries."""
    sample_text = "This paper discusses quantum computing and error mitigation."

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "<think>\nInternal analysis.\n</think>\nQuantum computing enables exponential speedups for specific computational problems."
    mock_resp.choices = [mock_choice]

    with patch("zocrates.rag.summary_generator.litellm.completion", return_value=mock_resp):
        with patch("zocrates.rag.summary_generator.get_secure_api_key", return_value="sk-test"):
            summary, source = generate_summary(sample_text, title="Quantum Computing")
            assert source == "generated"
            assert "<think>" not in summary
            assert "</think>" not in summary
            assert "Quantum computing enables exponential speedups" in summary


# ---------------------------------------------------------------------------
# 2. ToC Generator Tests
# ---------------------------------------------------------------------------


def test_generate_toc_full_document():
    markdown = """---
type: journalArticle
title: Attention Is All You Need
authors:
  - Vaswani, Ashish
year: 2017
---

# Attention Is All You Need

## 1 Introduction
The dominant sequence transduction models are based on complex neural networks.

```python
# Code block with a hash comment that must NOT become a heading
x = 10
```

## 2 Model Architecture
The Transformer follows an overall encoder-decoder architecture.

### 2.1 **Encoder and Decoder Stacks**
The encoder is composed of a stack of N = 6 identical layers.

### 2.2 [Attention Mechanism](https://arxiv.org/abs/1706.03762) &amp; `Scaled Dot-Product`
An attention function can be described as mapping a query and a set of key-value pairs.

<figure data-type="image" data-id="fig1">
![Architecture Diagram](images/fig_001.png)
**Figure 1.** The Transformer model architecture with multi-head attention and feed-forward sublayers.
</figure>

<figure data-type="table" data-csv="tables/table_001.csv">
**Table 1.** Maximum path lengths and per-layer complexity for different layer types.
</figure>

| Layer | Complexity | Sequential Ops |
| :--- | :--- | :--- |
| Self-Attention | O(1) | O(1) |
| Recurrent | O(n) | O(n) |

## 3 Conclusion
In this work, we presented the Transformer, the first sequence transduction model based entirely on attention.
"""

    abstract_text = "The Transformer is a novel simple architecture based solely on attention mechanisms."
    toc_json, toc_md = generate_toc(
        document_md=markdown,
        title="Attention Is All You Need",
        abstract=abstract_text,
        abstract_source="zotero",
        item_key="TESTITEM1",
    )

    # 1. Inspect toc.json
    assert toc_json["version"] == 1
    assert toc_json["item_key"] == "TESTITEM1"
    assert toc_json["parent_item_key"] is None
    assert toc_json["title"] == "Attention Is All You Need"
    assert toc_json["abstract"] == abstract_text
    assert toc_json["abstract_source"] == "zotero"

    sections = toc_json["sections"]
    assert len(sections) == 7

    # s0: Frontmatter
    assert sections[0]["id"] == "s0"
    assert sections[0]["heading"] == "Frontmatter"
    assert sections[0]["level"] == 0
    assert sections[0]["start_line"] == 1
    assert sections[0]["end_line"] == 7

    # s1: Title
    assert sections[1]["id"] == "s1"
    assert sections[1]["heading"] == "Attention Is All You Need"
    assert sections[1]["level"] == 1

    # s2: 1 Introduction
    assert sections[2]["id"] == "s2"
    assert sections[2]["heading"] == "1 Introduction"
    assert sections[2]["level"] == 2

    # s3: 2 Model Architecture
    assert sections[3]["id"] == "s3"
    assert sections[3]["heading"] == "2 Model Architecture"

    # s4: Normalized bold heading
    assert sections[4]["id"] == "s4"
    assert sections[4]["heading"] == "2.1 Encoder and Decoder Stacks"
    assert sections[4]["level"] == 3

    # s5: Normalized link, HTML entity, and code heading
    assert sections[5]["id"] == "s5"
    assert sections[5]["heading"] == "2.2 Attention Mechanism & Scaled Dot-Product"
    assert sections[5]["level"] == 3

    # s6: 3 Conclusion
    assert sections[6]["id"] == "s6"
    assert sections[6]["heading"] == "3 Conclusion"

    # Figures
    figures = toc_json["figures"]
    assert len(figures) == 1
    assert figures[0]["id"] == "f1"
    assert figures[0]["path"] == "images/fig_001.png"
    assert "Figure 1. The Transformer model architecture" in figures[0]["caption"]

    # Tables
    tables = toc_json["tables"]
    assert len(tables) == 2  # t1 (figure table) and t2 (standalone GFM table)
    assert tables[0]["id"] == "t1"
    assert tables[0]["path"] == "tables/table_001.csv"
    assert "Table 1. Maximum path lengths" in tables[0]["caption"]
    assert tables[1]["id"] == "t2"
    assert tables[1]["path"] is None

    # 2. Inspect toc.md
    assert "# Attention Is All You Need" in toc_md
    assert "## Abstract" in toc_md
    assert abstract_text in toc_md
    assert "- [s0] Frontmatter" in toc_md
    assert "- [s1] # Attention Is All You Need" in toc_md
    assert "- [s2] ## 1 Introduction" in toc_md
    assert "  - [s4] ### 2.1 Encoder and Decoder Stacks" in toc_md
    assert "## Figures" in toc_md
    assert "- [f1] Figure 1." in toc_md
    assert "## Tables" in toc_md
    assert "- [t1] Table 1." in toc_md


def test_generate_toc_caption_truncation_to_100_chars():
    markdown = """# Figure Test

<figure data-type="image">
<synthetic_caption>
This is an extremely long synthetic caption generated by a vision language model that describes every single aspect of the scatter plot including all data points, error bars, axes from 0 to 100, legends, colors, and baseline comparisons in tremendous detail.
</synthetic_caption>
![alt](images/scatter.png)
</figure>
"""
    toc_json, toc_md = generate_toc(markdown, title="Figure Test")
    fig = toc_json["figures"][0]
    assert len(fig["caption"]) <= 100
    assert fig["caption"].endswith("...")
    assert "[f1] This is an extremely long synthetic caption" in toc_md


def test_generate_toc_fallback_for_document_with_no_headings():
    markdown = """---
type: note
title: Plain Note
---

This is a simple plain text note without any ATX headings whatsoever. It spans multiple lines of body content.
"""
    toc_json, toc_md = generate_toc(markdown, title="Plain Note")
    sections = toc_json["sections"]
    assert len(sections) == 2
    assert sections[0]["id"] == "s0"
    assert sections[0]["heading"] == "Frontmatter"
    assert sections[1]["id"] == "s1"
    assert sections[1]["heading"] == "Document Body"
    assert sections[1]["level"] == 1

    assert "[s0] Frontmatter" in toc_md
    assert "[s1] # Document Body" in toc_md


def test_save_toc_files(tmp_path: Path):
    bundle_dir = tmp_path / "TESTBUNDLE"
    toc_json = {"version": 1, "item_key": "TESTBUNDLE", "sections": []}
    toc_md = "# Title\n\n## Abstract\nSample."

    save_toc_files(bundle_dir, toc_json, toc_md)

    assert (bundle_dir / "toc.json").exists()
    assert (bundle_dir / "toc.md").exists()

    loaded_json = json.loads((bundle_dir / "toc.json").read_text(encoding="utf-8"))
    assert loaded_json["item_key"] == "TESTBUNDLE"
    assert (bundle_dir / "toc.md").read_text(encoding="utf-8") == toc_md


def test_generate_toc_figure_alt_caption_only():
    markdown = """# Figure Alt Test

<figure data-type="image" data-id="fig-alt">
![Figure 1: Custom Alt Caption Diagram](images/pic_001.png)
</figure>
"""
    toc_json, toc_md = generate_toc(markdown, title="Figure Alt Test")
    figs = toc_json["figures"]
    assert len(figs) == 1
    assert figs[0]["id"] == "f1"
    assert figs[0]["caption"] == "Figure 1: Custom Alt Caption Diagram"
    assert figs[0]["path"] == "images/pic_001.png"
    assert "[f1] Figure 1: Custom Alt Caption Diagram" in toc_md


def test_generate_toc_document_without_frontmatter_starts_at_s1():
    markdown = """# Introduction
This is the introduction without frontmatter.

## 2 Methods
Here are the methods.
"""
    toc_json, toc_md = generate_toc(markdown, title="No Frontmatter Doc")
    sections = toc_json["sections"]
    assert len(sections) == 2
    assert sections[0]["id"] == "s1"
    assert sections[0]["heading"] == "Introduction"
    assert sections[0]["level"] == 1

    assert sections[1]["id"] == "s2"
    assert sections[1]["heading"] == "2 Methods"
    assert sections[1]["level"] == 2

    assert "[s0]" not in toc_md
    assert "[s1] # Introduction" in toc_md
    assert "[s2] ## 2 Methods" in toc_md


def test_generate_toc_frontmatter_only_no_body_overlap():
    markdown = """---
type: document
title: Only Frontmatter
authors:
  - Alice
---"""
    toc_json, toc_md = generate_toc(markdown, title="Only Frontmatter")
    sections = toc_json["sections"]
    assert len(sections) == 1
    assert sections[0]["id"] == "s0"
    assert sections[0]["heading"] == "Frontmatter"
    assert "[s0] Frontmatter" in toc_md
    assert "Document Body" not in toc_md


def test_generate_toc_headings_with_snake_case():
    markdown = """# Main

## 1 Setup for model_eval_pipeline_v2 in batch_mode
This section evaluates snake_case variables without mangling.

### 1.1 *Italicized* and _Another Italic_ and snake_case_var
Test combination of true italics and snake_case.
"""
    toc_json, toc_md = generate_toc(markdown, title="Snake Case Headings")
    sections = toc_json["sections"]
    assert sections[1]["heading"] == "1 Setup for model_eval_pipeline_v2 in batch_mode"
    assert sections[2]["heading"] == "1.1 Italicized and Another Italic and snake_case_var"


def test_generate_toc_explicit_parent_item_key():
    markdown = "# Simple Doc\n\n## 1 Section\nContent."
    toc_json, _ = generate_toc(
        document_md=markdown,
        title="Simple Doc",
        item_key="CHILD1",
        parent_item_key="PARENT99",
    )
    assert toc_json["item_key"] == "CHILD1"
    assert toc_json["parent_item_key"] == "PARENT99"


def test_generate_toc_frontmatter_fallback_parent_item_key():
    markdown = """---
type: journalArticle
item_key: ATTACH1
parent_item_key: PARENT_FROM_FM
title: Paper With Frontmatter
---

# Paper With Frontmatter

## 1 Intro
Content.
"""
    # When parent_item_key parameter is omitted (None), it falls back to frontmatter
    toc_json, _ = generate_toc(
        document_md=markdown,
        item_key="ATTACH1",
    )
    assert toc_json["item_key"] == "ATTACH1"
    assert toc_json["parent_item_key"] == "PARENT_FROM_FM"

    # When explicit parent_item_key is supplied, it overrides frontmatter
    toc_json_override, _ = generate_toc(
        document_md=markdown,
        item_key="ATTACH1",
        parent_item_key="EXPLICIT_PARENT",
    )
    assert toc_json_override["parent_item_key"] == "EXPLICIT_PARENT"


def test_generate_toc_malformed_frontmatter_graceful_handling():
    # Malformed YAML frontmatter (unclosed bracket/syntax error)
    markdown = """---
parent_item_key: [unclosed list
---

# Malformed Doc

## 1 Section
Content.
"""
    toc_json, _ = generate_toc(
        document_md=markdown,
        item_key="DOC_MALFORMED",
    )
    assert toc_json["item_key"] == "DOC_MALFORMED"
    assert toc_json["parent_item_key"] is None
    assert len(toc_json["sections"]) >= 2  # Frontmatter + Section 1



def test_save_toc_files_atomic_writes(tmp_path: Path):
    bundle_dir = tmp_path / "ATOMIC_BUNDLE"
    bundle_dir.mkdir()
    toc_json = {"version": 1, "item_key": "ATOMIC_BUNDLE", "sections": []}
    toc_md = "# Atomic Doc\n\n## Abstract\nContent."

    replaced_targets = []
    original_replace = os.replace

    def mock_replace(src, dst):
        replaced_targets.append(Path(dst).name)
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        save_toc_files(bundle_dir, toc_json, toc_md)

    assert (bundle_dir / "toc.md").exists()
    assert (bundle_dir / "toc.json").exists()
    assert replaced_targets == ["toc.md", "toc.json"]
    tmp_files = list(bundle_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# 3. DocumentProcessor Database & Pipeline Integration Tests
# ---------------------------------------------------------------------------


def test_document_processor_migration_and_abstract_storage(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    # Ensure database is initialized
    with _open_meta_db(cache_dir) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(conversion_meta)").fetchall()}

    assert "abstract" in cols
    assert "abstract_source" in cols


def test_document_processor_generates_toc_and_stores_abstract(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    source_file = tmp_path / "test_paper.md"
    source_file.write_text(
        "# Test Document\n\n## 1 Overview\nThis paper evaluates neural network architectures.\n",
        encoding="utf-8",
    )

    proc = DocumentProcessor(cache_dir)
    md = proc.get_or_convert(
        item_key="DOC123",
        source_path=source_file,
        content_type="text/markdown",
        title="Test Document",
        abstract="This paper evaluates neural network architectures.",
        parent_item_key="PARENT_DP_KEY",
    )

    assert md is not None
    item_bundle = cache_dir / "DOC123"
    assert (item_bundle / "document.md").exists()
    assert (item_bundle / "toc.json").exists()
    assert (item_bundle / "toc.md").exists()
    assert (item_bundle / "index.md").exists()

    # Verify toc.json content
    toc_data = json.loads((item_bundle / "toc.json").read_text(encoding="utf-8"))
    assert toc_data["item_key"] == "DOC123"
    assert toc_data["parent_item_key"] == "PARENT_DP_KEY"
    assert toc_data["abstract"] == "This paper evaluates neural network architectures."
    assert toc_data["abstract_source"] == "zotero"
    assert len(toc_data["sections"]) >= 2  # s0 Frontmatter, s1 Test Document, s2 1 Overview

    # Verify metadata retrieval
    meta = proc.get_metadata("DOC123")
    assert meta is not None
    assert meta["parent_item_key"] == "PARENT_DP_KEY"
    assert meta["abstract"] == "This paper evaluates neural network architectures."
    assert meta["abstract_source"] == "zotero"

    all_meta = proc.get_all_metadata()
    assert "DOC123" in all_meta
    assert all_meta["DOC123"]["abstract"] == "This paper evaluates neural network architectures."


def test_refresh_toc_parent_item_abstract_resolution(tmp_path: Path):
    """Verify refresh_toc correctly resolves parent item abstract for child attachments."""
    from zocrates.rag.zotero_db import ZoteroAttachment, ZoteroItem
    from scripts.refresh_toc import refresh_tocs

    zotero_dir = tmp_path / "Zotero"
    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    cache_dir.mkdir(parents=True)
    bundle_dir = cache_dir / "ATT123"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "document.md").write_text("# Paper Title\n\n## 1 Intro\nContent", encoding="utf-8")

    # Create dummy conversion_meta.db
    meta_db = cache_dir / "conversion_meta.db"
    with sqlite3.connect(str(meta_db)) as conn:
        conn.execute(
            """
            CREATE TABLE conversion_meta (
                item_key TEXT PRIMARY KEY,
                title TEXT,
                converter_id TEXT,
                converter_version TEXT,
                source_path TEXT,
                content_type TEXT,
                parent_item_type TEXT,
                converted_at TEXT,
                indexed_at TEXT,
                source_hash TEXT,
                char_count INTEGER,
                parent_item_key TEXT,
                parse_quality_score REAL,
                checkpoint_id TEXT,
                abstract TEXT,
                abstract_source TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, title, converter_id, converter_version, source_path, content_type, parent_item_key)
            VALUES ('ATT123', 'Paper Title', 'markitdown', '1.0', '/path/doc.pdf', 'application/pdf', NULL)
            """
        )

    # Create dummy zotero.sqlite
    (zotero_dir / "zotero.sqlite").touch()

    # Mock ZoteroDB
    mock_parent_item = ZoteroItem(
        item_id=10,
        item_key="PARENT999",
        item_type="journalArticle",
        title="Paper Title",
        abstract="Parent item abstract from Zotero metadata.",
    )
    mock_att = ZoteroAttachment(
        item_id=20,
        item_key="ATT123",
        parent_item_id=10,
        parent_item_key="PARENT999",
        content_type="application/pdf",
        path=Path("/path/doc.pdf"),
    )

    with patch("scripts.refresh_toc.ZoteroDB") as mock_zotero_db_cls:
        mock_db_instance = MagicMock()
        mock_zotero_db_cls.return_value = mock_db_instance
        # get_item_by_key(parent_key=None) returns None
        mock_db_instance.get_item_by_key.side_effect = lambda k: mock_parent_item if k == "PARENT999" else None
        # get_attachment_by_key returns the attachment pointing to parent_item_id=10
        mock_db_instance.get_attachment_by_key.return_value = mock_att
        mock_db_instance.get_item.return_value = mock_parent_item

        refresh_tocs(zotero_dir, target_keys=["ATT123"], dry_run=False)

    # Verify toc.json was written with parent abstract and source="zotero"
    assert (bundle_dir / "toc.json").exists()
    toc_data = json.loads((bundle_dir / "toc.json").read_text(encoding="utf-8"))
    assert toc_data["abstract"] == "Parent item abstract from Zotero metadata."
    assert toc_data["abstract_source"] == "zotero"
    assert toc_data["parent_item_key"] == "PARENT999"

