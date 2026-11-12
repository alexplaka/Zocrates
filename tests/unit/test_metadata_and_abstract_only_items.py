"""Unit tests for Metadata & Abstract-Only Items: Bundle Materialization, Graph Discovery, and Search.

Covers:
  - materialize_item_metadata_bundle with and without abstract.
  - Sanitization of JATS XML and markdown header demotion in abstracts.
  - Title fallback logic (DOI, URL, Untitled Item).
  - Virtual path resolution for zotero://item/.
  - PDF supersession of metadata bundles when child attachment is ingested.
  - POST /items/ingest endpoint behavior.
  - Document Frequency (DF) cap and automatic tag inclusion in sync_shared_tags.
  - evaluate_and_persist_edges guarding for abstract-less items.
  - Real library integration verification against 5WSCBQIL and KBLUYME2.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.agents.common.toolkit import _resolve_bundle_dir
from zocrates.rag.bundle_catalog import (
    generate_root_index_md,
    normalize_to_storage_relative,
    resolve_source_path,
)
from zocrates.rag.document_processor import (
    DocumentProcessor,
    _open_meta_db,
    compute_item_content_hash,
    materialize_item_metadata_bundle,
)
from zocrates.rag.graph_engine import (
    evaluate_and_persist_edges,
    sync_shared_tags,
)
from zocrates.rag.library_syncer import (
    LibrarySyncer,
    supersede_metadata_bundle_if_needed,
)
from zocrates.rag.zotero_db import ZoteroDB, ZoteroItem


# ---------------------------------------------------------------------------
# 1. Bundle Materialization Tests
# ---------------------------------------------------------------------------


class DummySyncer:
    def __init__(self, cache_dir: Path):
        self.processor = DocumentProcessor(cache_dir)
        self._zotero_data_dir = cache_dir


def test_materialize_metadata_bundle_with_abstract(tmp_path: Path):
    """Verify metadata bundle creation with abstract, JATS sanitization, and Notice outside abstract."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = DummySyncer(cache_dir)

    raw_jats_abstract = (
        "<jats:title>Abstract</jats:title>"
        "<jats:p>The transduction of many cellular stimuli results in oscillations in Ca2+.</jats:p>"
        "### In-abstract Header\n"
        "--- YAML-like fence ---\n"
        "Further detailed findings about synaptic plasticity."
    )
    item = ZoteroItem(
        item_id=118,
        item_key="5WSCBQIL",
        item_type="journalArticle",
        title="Sensitivity of CaM Kinase II to the Frequency of Ca 2+ Oscillations",
        creators=["Koninck, Paul De", "Schulman, Howard"],
        year=1998,
        publication="Science",
        doi="10.1126/science.279.5348.227",
        url="https://openalex.org/W2135011583",
        abstract=raw_jats_abstract,
    )

    bundle_dir, key = materialize_item_metadata_bundle(item, syncer, cache_dir=cache_dir)
    assert key == "5WSCBQIL"
    assert bundle_dir.exists()
    assert (bundle_dir / "document.md").exists()
    assert (bundle_dir / "toc.json").exists()
    assert (bundle_dir / "toc.md").exists()
    assert (bundle_dir / "index.md").exists()
    assert (bundle_dir / "item_relations.md").exists()

    doc_text = (bundle_dir / "document.md").read_text(encoding="utf-8")

    # Frontmatter assertions
    assert 'item_key: 5WSCBQIL' in doc_text or 'item_key: "5WSCBQIL"' in doc_text
    assert 'content_type: text/zotero-metadata' in doc_text or 'content_type: "text/zotero-metadata"' in doc_text
    assert 'path: zotero://item/5WSCBQIL' in doc_text or 'path: "zotero://item/5WSCBQIL"' in doc_text

    # Notice under Document Overview outside abstract
    assert "## Document Overview" in doc_text
    assert "> **Notice**: This Zotero entry does not have an attached full-text document" in doc_text
    assert "## Abstract" in doc_text

    # JATS tags stripped
    assert "<jats:" not in doc_text
    assert "</jats:" not in doc_text

    # Header demoted (no '### In-abstract Header')
    assert "### In-abstract Header" not in doc_text
    assert "In-abstract Header" in doc_text

    # Verify conversion_meta row
    meta = syncer.processor.get_metadata("5WSCBQIL")
    assert meta is not None
    assert meta["content_type"] == "text/zotero-metadata"
    assert meta["converter_id"] == "zotero_metadata"
    assert meta["abstract_source"] == "zotero"
    assert meta["abstract"] is not None
    assert meta["indexed_at"] is None  # stamped upon vector ingestion


def test_materialize_metadata_bundle_without_abstract(tmp_path: Path):
    """Verify metadata bundle creation for abstract-less item with Bibliographic Summary."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = DummySyncer(cache_dir)

    item = ZoteroItem(
        item_id=201,
        item_key="8X9Y1Z2A",
        item_type="journalArticle",
        title="A Survey of Graph Neural Networks",
        creators=["Wu, Zonghan", "Pan, Shirui"],
        year=2021,
        publication="IEEE TNNLS",
        doi="10.1109/TNNLS.2020.2978386",
        abstract="",
    )

    bundle_dir, key = materialize_item_metadata_bundle(item, syncer, cache_dir=cache_dir)
    assert key == "8X9Y1Z2A"
    doc_text = (bundle_dir / "document.md").read_text(encoding="utf-8")

    assert "## Document Overview" in doc_text
    assert "## Bibliographic Summary" in doc_text
    assert "- **Title**: A Survey of Graph Neural Networks" in doc_text
    assert "- **Authors**: Wu, Zonghan; Pan, Shirui" in doc_text
    assert "- **DOI**: 10.1109/TNNLS.2020.2978386" in doc_text

    meta = syncer.processor.get_metadata("8X9Y1Z2A")
    assert meta is not None
    assert meta["content_type"] == "text/zotero-metadata"
    assert meta["abstract_source"] == "none"
    assert meta["abstract"] is None


def test_materialize_title_fallbacks(tmp_path: Path):
    """Verify title fallback to DOI, URL, or Untitled Item when title is missing or whitespace."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = DummySyncer(cache_dir)

    # Fallback to DOI
    item_doi = ZoteroItem(
        item_id=301,
        item_key="NO_TITLE_DOI",
        item_type="journalArticle",
        title="   ",
        doi="10.1000/xyz123",
    )
    b_dir1, _ = materialize_item_metadata_bundle(item_doi, syncer, cache_dir=cache_dir)
    doc1 = (b_dir1 / "document.md").read_text(encoding="utf-8")
    assert "10.1000/xyz123" in doc1

    # Fallback to URL
    item_url = ZoteroItem(
        item_id=302,
        item_key="NO_TITLE_URL",
        item_type="webpage",
        title="",
        url="https://example.org/paper",
    )
    b_dir2, _ = materialize_item_metadata_bundle(item_url, syncer, cache_dir=cache_dir)
    doc2 = (b_dir2 / "document.md").read_text(encoding="utf-8")
    assert "https://example.org/paper" in doc2

    # Fallback to Untitled Item
    item_empty = ZoteroItem(
        item_id=303,
        item_key="EMPTY_ITEM",
        item_type="journalArticle",
        title="",
    )
    b_dir3, _ = materialize_item_metadata_bundle(item_empty, syncer, cache_dir=cache_dir)
    doc3 = (b_dir3 / "document.md").read_text(encoding="utf-8")
    assert "Untitled Item (EMPTY_ITEM)" in doc3


# ---------------------------------------------------------------------------
# 2. Virtual Path Resolution & Migration
# ---------------------------------------------------------------------------


def test_virtual_path_handling():
    """Verify zotero://item/ paths are treated as virtual URIs."""
    uri = "zotero://item/5WSCBQIL"
    assert normalize_to_storage_relative(uri) == uri
    resolved = resolve_source_path(uri, Path("/fake/zotero"))
    assert resolved == uri


def test_root_index_generation_with_item_uri(tmp_path: Path):
    """Verify generate_root_index_md formats zotero://item/ as [Zotero Item]."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta
                (item_key, title, converter_id, converter_version, source_path,
                 content_type, parent_item_type, converted_at, source_hash, char_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "5WSCBQIL",
                "CaM Kinase II",
                "zotero_metadata",
                "1.0",
                "zotero://item/5WSCBQIL",
                "text/zotero-metadata",
                "journalArticle",
                "2026-01-01T00:00:00Z",
                "abc",
                100,
            ),
        )
        conn.commit()

    generate_root_index_md(cache_dir)
    index_md = (cache_dir / "index.md").read_text(encoding="utf-8")
    assert "[Zotero Item](zotero://item/5WSCBQIL)" in index_md


# ---------------------------------------------------------------------------
# 3. PDF Supersession Tests
# ---------------------------------------------------------------------------


def test_pdf_supersession_purges_parent_metadata_bundle(tmp_path: Path):
    """Verify that when a child attachment is ingested, supersede_metadata_bundle_if_needed purges the parent's metadata bundle."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)

    parent_key = "PARENT1"
    att_key = "ATT1"

    syncer = MagicMock()
    processor = DocumentProcessor(cache_dir)
    syncer.processor = processor
    syncer._zotero_data_dir = tmp_path

    # 1. Materialize parent metadata bundle
    p_item = ZoteroItem(
        item_id=1,
        item_key=parent_key,
        item_type="journalArticle",
        title="Paper Title",
        abstract="Sample abstract",
    )
    materialize_item_metadata_bundle(p_item, syncer, cache_dir=cache_dir)

    assert (cache_dir / parent_key / "document.md").exists()
    assert processor.get_metadata(parent_key)["content_type"] == "text/zotero-metadata"

    # 2. Mock syncer.purge_item to call real processor.delete_cached
    def _mock_purge(key, regenerate_index=False):
        processor.delete_cached(key, update_root_index=False)

    syncer.purge_item.side_effect = _mock_purge

    # 3. Trigger supersession
    supersede_metadata_bundle_if_needed(parent_key, syncer)

    assert not (cache_dir / parent_key).exists()
    assert processor.get_metadata(parent_key) is None
    syncer.purge_item.assert_called_once_with(parent_key, regenerate_index=False)


# ---------------------------------------------------------------------------
# 4. Document Frequency (DF) Cap & Automatic Tags in sync_shared_tags
# ---------------------------------------------------------------------------


def test_sync_shared_tags_df_cap(tmp_path: Path):
    """Verify that tags appearing on >5% of items (minimum 5) are excluded as noise."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)

    # Initialize conversion_meta with 20 items
    meta_db = cache_dir / "conversion_meta.db"
    with _open_meta_db(cache_dir) as conn:
        for i in range(1, 21):
            conn.execute(
                """
                INSERT INTO conversion_meta
                    (item_key, title, converter_id, converter_version, source_path,
                     content_type, parent_item_type, converted_at, source_hash, char_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ITEM_{i:02d}",
                    f"Paper {i}",
                    "zotero_metadata",
                    "1.0",
                    f"zotero://item/ITEM_{i:02d}",
                    "text/zotero-metadata",
                    "journalArticle",
                    "2026-01-01T00:00:00Z",
                    "hash",
                    100,
                ),
            )
        conn.commit()

    # Create dummy zotero.sqlite with itemTags
    sqlite_path = zotero_dir / "zotero.sqlite"
    z_conn = sqlite3.connect(str(sqlite_path))
    z_conn.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER, type INTEGER);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER);
        """
    )
    for i in range(1, 21):
        z_conn.execute("INSERT INTO items VALUES (?, ?)", (i, f"ITEM_{i:02d}"))

    # Noise tag "Neurons" on 6 items (6/20 = 30% > 5% and > 5 items)
    z_conn.execute("INSERT INTO tags VALUES (1, 'Neurons')")
    for i in range(1, 7):
        z_conn.execute("INSERT INTO itemTags VALUES (?, 1, 1)", (i,))  # automatic tag

    # Substantive tag "CaM Kinase" on 3 items (3 <= 5 items, so retained)
    z_conn.execute("INSERT INTO tags VALUES (2, 'CaM Kinase')")
    z_conn.execute("INSERT INTO itemTags VALUES (1, 2, 1)")
    z_conn.execute("INSERT INTO itemTags VALUES (2, 2, 1)")
    z_conn.execute("INSERT INTO itemTags VALUES (3, 2, 1)")

    # Substantive tag "Synaptic Plasticity" on items 1 and 2
    z_conn.execute("INSERT INTO tags VALUES (3, 'Synaptic Plasticity')")
    z_conn.execute("INSERT INTO itemTags VALUES (1, 3, 1)")
    z_conn.execute("INSERT INTO itemTags VALUES (2, 3, 1)")

    z_conn.commit()
    z_conn.close()

    with _open_meta_db(cache_dir) as conn:
        with patch("zocrates.rag.graph_engine.get_graph_relations_config") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "include_automatic_tags_in_shared_tags": True,
                "min_shared_tags": 2,
                "shared_tags_score": 0.70,
            }
            n_created = sync_shared_tags(conn=conn, zotero_dir=zotero_dir, min_shared_tags=2)
            assert n_created >= 1

            # Check that ITEM_01 and ITEM_02 are connected via ('CaM Kinase', 'Synaptic Plasticity')
            # and NOT via 'Neurons'
            row = conn.execute(
                "SELECT match_evidence FROM item_relations WHERE source_key = 'ITEM_01' AND target_key = 'ITEM_02'"
            ).fetchone()
            assert row is not None
            evidence = json.loads(row["match_evidence"])
            shared = evidence.get("shared_tags", [])
            assert "CaM Kinase" in shared
            assert "Synaptic Plasticity" in shared
            assert "Neurons" not in shared  # Filtered by DF cap!


# ---------------------------------------------------------------------------
# 5. Abstract-Less Items Edge Evaluation Guarding
# ---------------------------------------------------------------------------


def test_evaluate_and_persist_edges_abstract_less_guard(tmp_path: Path):
    """Verify that evaluate_and_persist_edges skips hybrid search for abstract-less metadata items."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta
                (item_key, title, converter_id, converter_version, source_path,
                 content_type, parent_item_type, converted_at, source_hash, char_count, abstract, abstract_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "NO_ABS_ITEM",
                "Title Without Abstract",
                "zotero_metadata",
                "1.0",
                "zotero://item/NO_ABS_ITEM",
                "text/zotero-metadata",
                "journalArticle",
                "2026-01-01T00:00:00Z",
                "hash",
                100,
                None,
                "none",
            ),
        )
        conn.commit()

    (cache_dir / "NO_ABS_ITEM").mkdir(parents=True, exist_ok=True)

    syncer = MagicMock()
    syncer.processor._cache_dir = cache_dir
    syncer._zotero_data_dir = zotero_dir
    syncer.store = MagicMock()

    with patch("zocrates.rag.graph_engine.get_graph_relations_config") as mock_cfg:
        mock_cfg.return_value = {
            "enabled": True,
            "strategy": "hybrid_corroborated",
            "include_human_curated": False,
            "include_shared_tags": False,
            "sync_bundle_files": True,
        }
        res = evaluate_and_persist_edges("NO_ABS_ITEM", syncer, sync_bundle_files=True)
        # syncer.store.search should NOT be called because abstract is missing
        syncer.store.search.assert_not_called()
        assert (cache_dir / "NO_ABS_ITEM" / "item_relations.md").exists()


# ---------------------------------------------------------------------------
# 6. POST /items/ingest Endpoint
# ---------------------------------------------------------------------------


def test_post_items_ingest_endpoint():
    """Verify POST /items/ingest endpoint invokes syncer.sync_item for each key."""
    from zocrates.fast_api_app import app, verify_auth

    client = TestClient(app)
    mock_syncer = MagicMock()
    mock_syncer.sync_item = AsyncMock(return_value="processed")

    app.dependency_overrides[verify_auth] = lambda: True
    try:
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            resp = client.post(
                "/items/ingest",
                json={"item_keys": ["KEY_A", "KEY_B"]},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["results"] == {"KEY_A": "processed", "KEY_B": "processed"}
            assert mock_syncer.sync_item.call_count == 2
    finally:
        app.dependency_overrides.pop(verify_auth, None)


# ---------------------------------------------------------------------------
# 7. Verification Against Test-User Item 5WSCBQIL
# ---------------------------------------------------------------------------


def test_reproduction_item_5wscbqil(tmp_path: Path):
    """Verify ingestion, bundle materialization, and shared tags for 5WSCBQIL and KBLUYME2."""
    test_user_zotero = Path("/home/alex/Zotero/Zotero test-user")
    if not (test_user_zotero / "zotero.sqlite").exists():
        pytest.skip("Test user Zotero profile not available on host.")

    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)

    db = ZoteroDB(test_user_zotero / "zotero.sqlite", test_user_zotero / "storage")
    item_5w = db.get_item_by_key("5WSCBQIL")
    item_kb = db.get_item_by_key("KBLUYME2")
    assert item_5w is not None
    assert item_kb is not None

    syncer = DummySyncer(cache_dir)
    syncer.db = db

    # Materialize 5WSCBQIL
    b_dir_5w, _ = materialize_item_metadata_bundle(item_5w, syncer, cache_dir=cache_dir)
    assert (b_dir_5w / "document.md").exists()
    assert (b_dir_5w / "toc.json").exists()
    assert (b_dir_5w / "index.md").exists()

    # Materialize KBLUYME2
    b_dir_kb, _ = materialize_item_metadata_bundle(item_kb, syncer, cache_dir=cache_dir)
    assert (b_dir_kb / "document.md").exists()

    # Sync shared tags between both items
    with _open_meta_db(cache_dir) as conn:
        with patch("zocrates.rag.graph_engine.get_graph_relations_config") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "include_automatic_tags_in_shared_tags": True,
                "min_shared_tags": 2,
                "shared_tags_score": 0.70,
            }
            n = sync_shared_tags(conn=conn, zotero_dir=test_user_zotero, min_shared_tags=2)
            assert n >= 1

            # Verify edge in item_relations
            row = conn.execute(
                """
                SELECT match_evidence FROM item_relations
                WHERE (source_key = '5WSCBQIL' AND target_key = 'KBLUYME2')
                   OR (source_key = 'KBLUYME2' AND target_key = '5WSCBQIL')
                """
            ).fetchone()
            assert row is not None
            ev = json.loads(row["match_evidence"])
            shared = ev.get("shared_tags", [])
            # Expected shared tags: 'calcium spikes', 'calmodulin', 'synaptic plasticity'
            assert "calmodulin" in shared
            assert "calcium spikes" in shared
            assert "synaptic plasticity" in shared


def test_has_child_attachments_ignores_notes_and_metadata(tmp_path: Path):
    """Verify has_child_attachments ignores text/note and text/zotero-metadata rows."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    processor = DocumentProcessor(cache_dir=cache_dir)

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                                        content_type, converted_at, source_hash, char_count, parent_item_key)
            VALUES ('NOTE_KEY', 'note_conv', '1.0', 'zotero://note/NOTE_KEY', 'text/note', '2026-01-01', 'h1', 10, 'PARENT_KEY'),
                   ('META_KEY', 'meta_conv', '1.0', 'zotero://item/META_KEY', 'text/zotero-metadata', '2026-01-01', 'h2', 10, 'PARENT_KEY')
            """
        )

    # Note and metadata rows do not count as file attachments
    assert processor.has_child_attachments("PARENT_KEY") is False

    # Actual file attachment row does count
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                                        content_type, converted_at, source_hash, char_count, parent_item_key)
            VALUES ('PDF_KEY', 'markitdown', '1.0', 'storage/PDF_KEY/doc.pdf', 'application/pdf', '2026-01-01', 'h3', 10, 'PARENT_KEY')
            """
        )
    assert processor.has_child_attachments("PARENT_KEY") is True


@pytest.mark.asyncio
async def test_sync_item_dispatches_notes_and_attachments(tmp_path: Path):
    """Verify sync_item dispatches notes and attachments to respective methods even if get_item_by_key returns them."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = DummySyncer(cache_dir)
    syncer._ingest_item = AsyncMock(return_value="item_processed")
    syncer._ingest_standalone_note = AsyncMock(return_value="note_processed")
    syncer._ingest_standalone_attachment = AsyncMock(return_value="att_processed")

    mock_db = MagicMock()
    # Note item returned by get_item_by_key
    note_item = MagicMock(item_id=1, item_key="N1", item_type="note", title="Note")
    mock_db.get_item_by_key.return_value = note_item
    mock_db.get_attachment_by_key.return_value = None
    mock_db.get_note_by_key.return_value = MagicMock(note_id=1, note_key="N1")
    syncer.db = mock_db

    res = await LibrarySyncer.sync_item(syncer, "N1")
    assert res == "note_processed"
    syncer._ingest_standalone_note.assert_awaited_once()
    syncer._ingest_item.assert_not_awaited()


def test_resolve_bundle_dir_metadata_on_demand_and_notices(tmp_path: Path):
    """Verify _resolve_bundle_dir materializes metadata bundle on demand and attaches note notices."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = DummySyncer(cache_dir)

    mock_db = MagicMock()
    mock_item = MagicMock(
        item_id=10,
        item_key="BARE_ITEM",
        item_type="journalArticle",
        title="Bare Item",
        abstract="Test Abstract",
        creators=["Author A"],
        year=2024,
        publication="Test Pub",
        doi=None,
        url=None,
        tags=[],
        collections=[],
        relations=[],
    )
    mock_db.is_item_active.return_value = True
    mock_db.get_note_by_key.return_value = None
    mock_db.get_item_by_key.return_value = mock_item
    mock_db.get_attachments.return_value = []
    mock_db.get_notes_for_item.return_value = [
        MagicMock(note_key="CHILD_NOTE_1", title="Child Note 1")
    ]
    syncer.db = mock_db

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=syncer):
        bundle_dir, resolved_key, notice = _resolve_bundle_dir("BARE_ITEM")
        assert bundle_dir == cache_dir / "BARE_ITEM"
        assert resolved_key == "BARE_ITEM"
        assert notice is not None
        assert "Child Note 1" in notice
        assert (bundle_dir / "document.md").exists()

