"""Unit tests for Graph Paper Relations: Defensive Bundle Guardrails & Workflow Harmonization.

Verifies:
- P0-0: Minimal Storage Schema Bootstrap (keywords column, item_relations table and indexes)
- P0-1: Frontmatter Refresher Data Preservation (keywords in build_frontmatter & refresher)
- P0-2: Note Bundle Resiliency (index.md and item_relations.md in materialize_note_bundle)
- P0-3: Universal item_relations.md Materialization (0 bytes for orphans, index.md catalog link)
- P0-4: Neighbor Invalidation on Deletion (delete edges and regenerate neighbor bundles)
- P1-1: INSERT OR REPLACE Column Nulling Guard (keywords preserved across re-saves)
- P1-2: Note Schema Isolation (refresh_bundle_frontmatter skips notes)
- P1-3: TOC Dynamic Count Isolation (dynamic **Related Items**: N count, cached_tocs isolation)
- P1-4: Atomic File Replacement in regenerate_item_relations_md
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from zocrates.rag.bundle_catalog import (
    build_frontmatter,
    generate_item_relations_md,
    regenerate_item_relations_md,
)
from zocrates.rag.document_processor import (
    DocumentProcessor,
    _INITIALIZED_META_DBS,
    _open_meta_db,
    materialize_note_bundle,
)
from zocrates.rag.frontmatter_refresher import refresh_bundle_frontmatter
from zocrates.rag.zotero_db import ZoteroNote


def test_schema_bootstrap_and_migration(tmp_path: Path):
    """P0-0: Test that new DBs and migrated legacy DBs have keywords and item_relations."""
    cache_dir = tmp_path / "legacy_cache"
    cache_dir.mkdir(parents=True)
    db_path = cache_dir / "conversion_meta.db"

    # 1. Create a legacy database without keywords column and without item_relations table
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE conversion_meta (
            item_key TEXT PRIMARY KEY,
            title TEXT,
            converter_id TEXT NOT NULL,
            converter_version TEXT NOT NULL,
            source_path TEXT NOT NULL,
            content_type TEXT NOT NULL,
            parent_item_type TEXT,
            converted_at TEXT NOT NULL,
            indexed_at TEXT,
            source_hash TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            parent_item_key TEXT,
            parse_quality_score REAL,
            checkpoint_id TEXT,
            abstract TEXT,
            abstract_source TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # Clear memory cache so _open_meta_db runs initialization and migration
    _INITIALIZED_META_DBS.discard(cache_dir)

    with _open_meta_db(cache_dir) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(conversion_meta)").fetchall()}
        assert "keywords" in cols

        # Check item_relations table
        rel_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='item_relations'"
        ).fetchone()
        assert rel_table is not None

        # Check indexes
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_relations_source_score" in indexes or "idx_relations_source" in indexes
        assert "idx_relations_target" in indexes


def test_build_frontmatter_keywords_preservation(tmp_path: Path):
    """P0-1: Test build_frontmatter serializes keywords list when provided."""
    dummy_source = tmp_path / "doc.pdf"
    dummy_source.write_bytes(b"%PDF-1.4")

    fm_str = build_frontmatter(
        item_key="KEY1",
        parent_item_type="journalArticle",
        title="Sample Paper",
        collections=None,
        source_path=dummy_source,
        source_hash="sha256hash",
        content_type="application/pdf",
        converter_id="docling",
        converter_version="1.0",
        converted_at="2026-03-30T00:00:00Z",
        keywords=["graph rag", "paper relations", "knowledge graphs"],
    )

    parsed = yaml.safe_load(fm_str.strip("- \n"))
    assert parsed["keywords"] == ["graph rag", "paper relations", "knowledge graphs"]

    # Test empty or None keywords omits key
    fm_str_empty = build_frontmatter(
        item_key="KEY2",
        parent_item_type="journalArticle",
        title="Sample Paper 2",
        collections=None,
        source_path=dummy_source,
        source_hash="sha256hash",
        content_type="application/pdf",
        converter_id="docling",
        converter_version="1.0",
        converted_at="2026-03-30T00:00:00Z",
        keywords=None,
    )
    parsed_empty = yaml.safe_load(fm_str_empty.strip("- \n"))
    assert "keywords" not in parsed_empty


def test_generate_item_relations_md_formatting(tmp_path: Path):
    """P1-4: Test generate_item_relations_md formatting, sorting, and orphan 0-byte output."""
    # Orphan case: empty relations -> 0 bytes
    orphan_md = generate_item_relations_md("ORPHAN1", [])
    assert orphan_md == ""

    cache_dir = tmp_path / "cache"
    (cache_dir / "KEY_A").mkdir(parents=True)
    (cache_dir / "KEY_A" / "document.md").write_text("# Paper A\n", encoding="utf-8")
    (cache_dir / "KEY_B").mkdir(parents=True)
    (cache_dir / "KEY_B" / "document.md").write_text("# Paper B\n", encoding="utf-8")
    # KEY_C is intentionally unmaterialized to verify unlinked rendering

    # Non-empty relations
    relations = [
        {"target_key": "KEY_B", "score": 0.854, "relation_type": "hybrid_search"},
        {"target_key": "KEY_A", "score": 1.0, "relation_type": "human_curated"},
        {"target_key": "KEY_B", "score": 0.70, "relation_type": "shared_tags"},  # duplicate target, lower score
        {"target_key": "KEY_C", "score": 0.854, "relation_type": "hybrid_search"},
    ]
    titles = {
        "KEY_A": "Paper Alpha",
        "KEY_B": "Paper Beta",
    }  # KEY_C has no title in map, should fallback to KEY_C

    rendered = generate_item_relations_md("SOURCE1", relations, titles, cache_dir=cache_dir)
    assert rendered.startswith("# Related Items: SOURCE1\n\n")

    # KEY_A should be first (score 1.0) and linked
    lines = rendered.splitlines()
    assert "- [KEY_A - Paper Alpha](../KEY_A/document.md)" in lines
    assert "  - Relation: `human_curated` (score: 1.0)" in lines

    # KEY_B coalesced to 0.85, KEY_C at 0.85
    assert "- [KEY_B - Paper Beta](../KEY_B/document.md)" in lines
    assert "  - Relation: `hybrid_search` (score: 0.85)" in lines

    # KEY_C has no converted bundle, so renders as unlinked reference
    assert "- **[KEY_C] KEY_C**" in lines


def test_regenerate_item_relations_md_and_catalog(tmp_path: Path):
    """P0-3 & P1-4: Test regenerate_item_relations_md creates item_relations.md and updates index.md."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "BUNDLE1"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "document.md").write_text("# Title\nBody", encoding="utf-8")

    # 1. Orphan bundle with no conversion_meta.db
    rel_path = regenerate_item_relations_md("BUNDLE1", cache_dir)
    assert rel_path is not None
    assert rel_path.exists()
    assert rel_path.read_text(encoding="utf-8") == ""  # 0 bytes

    # Check index.md has related items link
    index_md = (bundle_dir / "index.md").read_text(encoding="utf-8")
    assert "- **Related Items**: [item_relations.md](./item_relations.md)" in index_md

    # 2. Add relation to conversion_meta.db
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                content_type, parent_item_type, converted_at, source_hash, char_count, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BUNDLE1", "direct_read", "1.0", "storage/BUNDLE1", "text/markdown", "doc", "now", "h1", 10, "Bundle One"),
        )
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                content_type, parent_item_type, converted_at, source_hash, char_count, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("TARGET1", "direct_read", "1.0", "storage/TARGET1", "text/markdown", "doc", "now", "h2", 10, "Target One"),
        )
        conn.execute(
            """
            INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("BUNDLE1", "TARGET1", 0.95, "hybrid_search", "{}", "now"),
        )
        conn.commit()

    (cache_dir / "TARGET1").mkdir(parents=True)
    (cache_dir / "TARGET1" / "document.md").write_text("# Target One\n", encoding="utf-8")

    rel_path2 = regenerate_item_relations_md("BUNDLE1", cache_dir)
    assert rel_path2 == rel_path
    content = rel_path2.read_text(encoding="utf-8")
    assert "# Related Items: BUNDLE1" in content
    assert "- [TARGET1 - Target One](../TARGET1/document.md)" in content
    assert "  - Relation: `hybrid_search` (score: 0.95)" in content


def test_note_bundle_resiliency_and_isolation(tmp_path: Path):
    """P0-2 & P1-2: Test note bundle materializes relations & index.md, and refresher skips notes."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)

    note = ZoteroNote(
        note_id=1,
        note_key="NOTE1",
        parent_item_id=10,
        parent_item_key="PARENT10",
        title="My Research Note",
        note_html="<p>Important literature notes.</p>",
    )

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir

    note_dir, resolved_key = materialize_note_bundle(note, mock_syncer, cache_dir)
    assert resolved_key == "NOTE1"
    assert note_dir.exists()

    # P0-2: Verify item_relations.md and index.md are generated
    assert (note_dir / "item_relations.md").exists()
    assert (note_dir / "index.md").exists()

    index_text = (note_dir / "index.md").read_text(encoding="utf-8")
    assert "- **Related Items**: [item_relations.md](./item_relations.md)" in index_text

    # P1-2: Verify refresh_bundle_frontmatter skips notes
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()

    # Create minimal zotero.sqlite
    with sqlite3.connect(str(sqlite_path)) as zconn:
        zconn.execute("CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, libraryID INTEGER, dateModified TEXT);")
        zconn.commit()

    orig_note_doc = (note_dir / "document.md").read_text(encoding="utf-8")
    stats = refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        cache_dir=cache_dir,
        target_keys=["NOTE1"],
    )
    assert stats["skipped"] == 1
    assert stats["updated"] == 0
    # Note document.md remains completely untouched
    assert (note_dir / "document.md").read_text(encoding="utf-8") == orig_note_doc


def test_insert_or_replace_keywords_nulling_guard(tmp_path: Path):
    """P1-1: Verify _save_to_cache and materialize_note_bundle preserve existing keywords."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    processor = DocumentProcessor(cache_dir)

    source = tmp_path / "paper.txt"
    source.write_text("Hello World Content", encoding="utf-8")

    # Initial convert
    processor.get_or_convert("KEY_KW", source, "text/plain", title="Initial Title")

    # Manually populate keywords in DB (simulating Phase 1 extractor)
    kw_json = json.dumps(["quantum computing", "error correction"])
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            "UPDATE conversion_meta SET keywords = ? WHERE item_key = ?",
            (kw_json, "KEY_KW"),
        )
        conn.commit()

    # Re-save/re-convert
    processor.convert_with("KEY_KW", "direct_read", source, "text/plain")

    # Verify keywords column was NOT nulled out
    with _open_meta_db(cache_dir) as conn:
        row = conn.execute(
            "SELECT keywords FROM conversion_meta WHERE item_key = ?",
            ("KEY_KW",),
        ).fetchone()
        assert row is not None
        assert row["keywords"] == kw_json


def test_delete_cached_neighbor_invalidation(tmp_path: Path):
    """P0-4: Verify delete_cached removes relations and regenerates neighbor bundles."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    processor = DocumentProcessor(cache_dir)

    # Setup Paper A and Paper B bundles
    src_a = tmp_path / "a.txt"
    src_a.write_text("Paper A content", encoding="utf-8")
    src_b = tmp_path / "b.txt"
    src_b.write_text("Paper B content", encoding="utf-8")

    processor.get_or_convert("PAPER_A", src_a, "text/plain", title="Paper A")
    processor.get_or_convert("PAPER_B", src_b, "text/plain", title="Paper B")

    # Create bidirectional edge between A and B
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("PAPER_A", "PAPER_B", 0.9, "hybrid_search", "{}", "now"),
        )
        conn.execute(
            """
            INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("PAPER_B", "PAPER_A", 0.9, "hybrid_search", "{}", "now"),
        )
        conn.commit()

    # Regenerate B to reflect relation to A
    regenerate_item_relations_md("PAPER_B", cache_dir)
    b_rel = (cache_dir / "PAPER_B" / "item_relations.md").read_text(encoding="utf-8")
    assert "PAPER_A - Paper A" in b_rel

    # Delete Paper A
    deleted = processor.delete_cached("PAPER_A")
    assert deleted is True

    # 1. Relations for PAPER_A should be deleted from DB
    with _open_meta_db(cache_dir) as conn:
        rows = conn.execute(
            "SELECT * FROM item_relations WHERE source_key = 'PAPER_A' OR target_key = 'PAPER_A'"
        ).fetchall()
        assert len(rows) == 0

    # 2. Paper B's item_relations.md should have been regenerated without Paper A
    b_rel_after = (cache_dir / "PAPER_B" / "item_relations.md").read_text(encoding="utf-8")
    assert "PAPER_A" not in b_rel_after
    assert b_rel_after == ""  # B is now an orphan (0 bytes)


def test_toc_dynamic_count_isolation(tmp_path: Path):
    """P1-3: Verify get_document_toc appends Related Items count without polluting cached_tocs."""
    from unittest.mock import patch
    from google.adk.tools import ToolContext
    from zocrates.agents.common.tools import get_document_toc
    from zocrates.agents.common.state_keys import CACHED_TOCS

    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ITEM_TOC"
    bundle_dir.mkdir(parents=True)

    # Document and ToC files
    doc_content = "# Test Doc\n\nAbstract here.\n\n## Table of Contents\n- [s0] Frontmatter\n- [s1] Intro"
    (bundle_dir / "document.md").write_text(doc_content, encoding="utf-8")
    toc_content = "## Table of Contents\n- [s0] Frontmatter\n- [s1] Intro"
    (bundle_dir / "toc.md").write_text(toc_content, encoding="utf-8")

    # Add 2 relations in conversion_meta.db
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                content_type, parent_item_type, converted_at, source_hash, char_count, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("ITEM_TOC", "direct_read", "1.0", "storage/ITEM_TOC", "text/markdown", "doc", "now", "h", 10, "Test Doc"),
        )
        conn.execute(
            """
            INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("ITEM_TOC", "NEIGHBOR1", 0.9, "hybrid_search", "{}", "now"),
        )
        conn.execute(
            """
            INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("ITEM_TOC", "NEIGHBOR2", 0.8, "hybrid_search", "{}", "now"),
        )
        conn.commit()

    mock_context = MagicMock(spec=ToolContext)
    mock_context.state = {}

    with patch("zocrates.agents.common.tools._resolve_bundle_dir") as mock_resolve, \
         patch("zocrates.agents.common.tools.get_document_toc_core") as mock_core:
        mock_resolve.return_value = (bundle_dir, "ITEM_TOC", None)
        mock_core.return_value = {
            "status": "success",
            "item_key": "ITEM_TOC",
            "title": "Test Doc",
            "toc_content": f"# Test Doc\n\nAbstract text.\n\n{toc_content}",
        }

        res = get_document_toc("ITEM_TOC", tool_context=mock_context)
        assert res["status"] == "success"

        # 1. Output toc_content has dynamic Related Items count
        assert "**Related Items**: 2" in res["toc_content"]

        # 2. cached_tocs does NOT contain **Related Items**
        cached = mock_context.state.get(CACHED_TOCS, {})
        assert "ITEM_TOC" in cached
        outline = cached["ITEM_TOC"]["outline_text"]
        assert "**Related Items**" not in outline


def test_refresh_bundle_frontmatter_preserves_keywords(tmp_path: Path):
    """P0-1: Verify refresh_bundle_frontmatter preserves existing keywords when metadata updates."""
    from tests.unit.test_frontmatter_refresher import _add_zotero_article, _init_mock_zotero_db

    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    # Initial metadata in Zotero
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="PARENT1",
        title="Updated Title from Zotero",
        creators=[("Alice", "Smith")],
        year="2022",
        doi="10.1234/initial",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "ATT1"
    bundle_dir.mkdir(parents=True)

    kw_list = ["graph relations", "literature traversal"]
    # Seed DB with existing keywords
    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title, keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ATT1",
                "markitdown",
                "1.0",
                "storage/ATT1/doc.pdf",
                "application/pdf",
                "journalArticle",
                "2026-01-01T00:00:00Z",
                "hash123",
                100,
                "PARENT1",
                "Old Title",
                json.dumps(kw_list),
            ),
        )
        meta_conn.commit()

    initial_doc = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: ATT1\n"
        "parent_item_key: PARENT1\n"
        "title: Old Title\n"
        "keywords:\n"
        "- graph relations\n"
        "- literature traversal\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/ATT1/doc.pdf\n"
        "  hash: hash123\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-01-01T00:00:00Z'\n"
        "---\n\n"
        "# Old Title\n\nSome body content.\n"
    )
    (bundle_dir / "document.md").write_text(initial_doc, encoding="utf-8")

    stats = refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        cache_dir=cache_dir,
        target_keys=["ATT1"],
    )
    assert stats["updated"] == 1
    assert stats["skipped"] == 0

    updated_doc = (bundle_dir / "document.md").read_text(encoding="utf-8")
    assert "title: Updated Title from Zotero" in updated_doc
    assert "keywords:\n- graph relations\n- literature traversal" in updated_doc

    # Run again: no-op check should match normalized frontmatter including keywords
    stats_noop = refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        cache_dir=cache_dir,
        target_keys=["ATT1"],
    )
    assert stats_noop["updated"] == 0
    assert stats_noop["skipped"] == 1

