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

    # Attachment item returned by get_attachment_by_key
    mock_db.get_attachment_by_key.return_value = MagicMock(item_id=2, item_key="ATT1")
    res_att = await LibrarySyncer.sync_item(syncer, "ATT1")
    assert res_att == "att_processed"
    syncer._ingest_standalone_attachment.assert_awaited_once()


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

        # Call a second time when bundle already exists to verify Step 3 fast-path returns fresh bundle
        b2, k2, n2 = _resolve_bundle_dir("BARE_ITEM")
        assert b2 == bundle_dir
        assert k2 == resolved_key
        assert n2 is not None
        assert "Child Note 1" in n2

        # Step 3 exception branch when materialize_item_metadata_bundle fails
        with patch("zocrates.rag.document_processor.materialize_item_metadata_bundle", side_effect=Exception("Bundle fail")):
            res3 = _resolve_bundle_dir("BARE_ITEM_FAIL")
            assert res3 is None


# ---------------------------------------------------------------------------
# 8. Diff Coverage Edge Cases Tests
# ---------------------------------------------------------------------------


def test_post_items_ingest_edge_cases():
    """Verify POST /items/ingest error branches: not registered, blank keys, exceptions."""
    from zocrates.fast_api_app import app, verify_auth

    client = TestClient(app)
    app.dependency_overrides[verify_auth] = lambda: True
    try:
        # 1. Not registered -> 400
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            resp = client.post("/items/ingest", json={"item_keys": ["KEY_A"]})
            assert resp.status_code == 400
            assert "Zocrates backend is not registered" in resp.json()["detail"]

        # 2. Blank keys and sync_item exception
        mock_syncer = MagicMock()
        mock_syncer.sync_item = AsyncMock(side_effect=Exception("Disk full"))
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            resp = client.post("/items/ingest", json={"item_keys": ["   ", "KEY_ERR"]})
            assert resp.status_code == 200
            data = resp.json()
            assert "   " not in data["results"]
            assert data["results"]["KEY_ERR"] == "error: Disk full"
    finally:
        app.dependency_overrides.pop(verify_auth, None)


def test_convert_item_regular_item_without_attachments():
    """Verify POST /convert/{item_key} handles regular items with no attachments on disk."""
    from zocrates.fast_api_app import app, verify_auth

    client = TestClient(app)
    mock_syncer = MagicMock()
    mock_syncer.db.get_attachment_by_key.return_value = None
    mock_item = MagicMock(item_id=42, item_key="REG_ITEM", item_type="journalArticle")
    mock_syncer.db.get_item_by_key.return_value = mock_item
    mock_syncer.db.get_attachments.return_value = []
    mock_syncer.sync_item = AsyncMock(return_value="processed")

    app.dependency_overrides[verify_auth] = lambda: True
    try:
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            resp = client.post("/convert/REG_ITEM")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "processed"
            assert data["converter_id"] == "zotero_metadata"
            assert data["content_type"] == "text/zotero-metadata"
            mock_syncer.sync_item.assert_awaited_once_with(item_key="REG_ITEM", force=False)
    finally:
        app.dependency_overrides.pop(verify_auth, None)


def test_batch_convert_skips_and_supersedes(tmp_path: Path):
    """Verify batch_convert supersedes parent metadata bundle when skipping an already converted item."""
    from zocrates.fast_api_app import app, verify_auth
    from zocrates.rag.document_processor import _file_sha256
    from zocrates.rag.zotero_db import ZoteroAttachment

    mock_syncer = MagicMock()
    att_file = tmp_path / "doc.pdf"
    att_file.write_bytes(b"%PDF-1.4 test")
    att = ZoteroAttachment(
        item_id=1,
        item_key="ATT_1",
        parent_item_id=10,
        parent_item_key="PARENT_P1",
        content_type="application/pdf",
        path=att_file,
    )
    mock_syncer.db.get_all_attachments.return_value = [att]
    mock_syncer.db.get_attachment_by_key.return_value = att
    mock_syncer.db.get_item_title.return_value = "Paper 1"
    mock_syncer.db.get_item.return_value = MagicMock(
        title="Paper 1", creators=None, year=None, publication=None, doi=None, url=None, abstract=None
    )
    file_hash = _file_sha256(att_file)
    mock_syncer.processor.get_metadata.return_value = {
        "converter_id": "markitdown",
        "source_hash": file_hash,
    }
    mock_syncer.processor._document_converter_id = "markitdown"
    mock_syncer.processor.datalab_non_pdf_fallback = None

    client = TestClient(app)
    app.dependency_overrides[verify_auth] = lambda: True
    try:
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.rag.library_syncer.supersede_metadata_bundle_if_needed") as mock_super:
                resp = client.post(
                    "/batch-convert",
                    json={"item_keys": ["ATT_1"], "converter_id": "markitdown", "force": False},
                )
                assert resp.status_code == 200
                mock_super.assert_called_once_with("PARENT_P1", mock_syncer)
    finally:
        app.dependency_overrides.pop(verify_auth, None)


def test_convert_item_attachment_supersedes_metadata(tmp_path: Path):
    """Verify POST /convert/{item_key} calls supersede_metadata_bundle_if_needed when converting attachment."""
    from zocrates.fast_api_app import app, verify_auth
    from zocrates.rag.zotero_db import ZoteroAttachment

    client = TestClient(app)
    mock_syncer = MagicMock()
    att_file = tmp_path / "paper.pdf"
    att_file.write_bytes(b"%PDF-1.4 file")
    att = ZoteroAttachment(
        item_id=10,
        item_key="ATT_KEY_1",
        parent_item_id=5,
        parent_item_key="PARENT_P5",
        content_type="application/pdf",
        path=att_file,
    )
    mock_syncer.db.get_attachment_by_key.return_value = att
    mock_syncer.processor.convert_with.return_value = "# Markdown\n\nBody content."
    mock_syncer.processor._document_converter_id = "markitdown"
    mock_syncer.processor.datalab_non_pdf_fallback = None

    app.dependency_overrides[verify_auth] = lambda: True
    try:
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.fast_api_app._dispatch_edge_evaluation"):
                with patch("zocrates.rag.library_syncer.supersede_metadata_bundle_if_needed") as mock_super:
                    resp = client.post("/convert/ATT_KEY_1")
                    assert resp.status_code == 200
                    mock_super.assert_called_once_with("PARENT_P5", mock_syncer)
    finally:
        app.dependency_overrides.pop(verify_auth, None)


def test_batch_convert_full_conversion_supersedes(tmp_path: Path):
    """Verify batch_convert supersedes parent metadata bundle when completing conversion with force=True."""
    from sse_starlette.sse import AppStatus
    from zocrates.fast_api_app import app, verify_auth
    from zocrates.rag.zotero_db import ZoteroAttachment

    AppStatus.should_exit_event = None

    mock_syncer = MagicMock()
    att_file = tmp_path / "doc.pdf"
    att_file.write_bytes(b"%PDF-1.4 test")
    att = ZoteroAttachment(
        item_id=1,
        item_key="ATT_FORCE",
        parent_item_id=10,
        parent_item_key="PARENT_FORCE",
        content_type="application/pdf",
        path=att_file,
    )
    mock_syncer.db.get_attachment_by_key.return_value = att
    mock_syncer.db.get_item_title.return_value = "Paper Force"
    mock_syncer.db.get_item.return_value = MagicMock(
        title="Paper Force", creators=None, year=None, publication=None, doi=None, url=None, abstract=None
    )
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "ATT_FORCE").mkdir(parents=True, exist_ok=True)
    mock_syncer.processor._cache_dir = corpus_dir
    mock_syncer.processor.convert_with.return_value = (
        "# Heading\n\n"
        "This is a substantive paragraph with ample text describing scientific findings "
        "in thorough detail so that the chunk markdown utility generates valid chunk objects."
    )
    mock_syncer.processor.get_metadata.return_value = {"keywords": []}
    mock_syncer.processor._document_converter_id = "markitdown"
    mock_syncer.processor.datalab_non_pdf_fallback = None

    client = TestClient(app)
    app.dependency_overrides[verify_auth] = lambda: True
    try:
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.fast_api_app._dispatch_edge_evaluation"):
                with patch("zocrates.rag.library_syncer.supersede_metadata_bundle_if_needed") as mock_super:
                    resp = client.post(
                        "/batch-convert",
                        json={"item_keys": ["ATT_FORCE"], "converter_id": "markitdown", "force": True},
                    )
                    assert resp.status_code == 200
                    mock_super.assert_called_once_with("PARENT_FORCE", mock_syncer)
    finally:
        app.dependency_overrides.pop(verify_auth, None)


@pytest.mark.asyncio
async def test_library_syncer_metadata_edge_evaluation_failure(tmp_path: Path):
    """Verify _ingest_item handles edge evaluation failures and already ingested attachments gracefully."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    syncer = LibrarySyncer.__new__(LibrarySyncer)
    syncer._zotero_data_dir = tmp_path
    syncer.processor = DocumentProcessor(cache_dir)
    syncer.store = MagicMock()
    syncer.db = MagicMock()

    # 1. Edge evaluation failure during metadata item ingestion
    item = ZoteroItem(
        item_id=505,
        item_key="META_FAIL_EDGE",
        item_type="journalArticle",
        title="Meta Edge Fail",
        abstract="Some abstract text",
        creators=["Author E"],
        year=2024,
        publication=None,
        doi=None,
        url=None,
        tags=[],
        collections=[],
        relations=[],
    )
    syncer.db.get_item.return_value = item
    syncer.db.get_attachments.return_value = []
    syncer.db.get_notes_for_item.return_value = []
    syncer.store.is_ingested.return_value = False

    with patch("zocrates.rag.graph_engine.evaluate_and_persist_edges", side_effect=Exception("Edge fail")):
        res = await syncer._ingest_item(505, "META_FAIL_EDGE", "Meta Edge Fail", force=True, evaluate_edges=True)
        assert res == "processed"

    # 2. Already ingested attachment supersedes metadata bundle
    att_file = tmp_path / "existing.pdf"
    att_file.write_bytes(b"%PDF-1.4 file")
    from zocrates.rag.zotero_db import ZoteroAttachment
    att = ZoteroAttachment(
        item_id=606,
        item_key="ATT_EXISTING",
        parent_item_id=505,
        parent_item_key="META_FAIL_EDGE",
        content_type="application/pdf",
        path=att_file,
    )
    syncer.db.get_attachments.return_value = [att]
    syncer.store.is_ingested.return_value = True
    with patch("zocrates.rag.library_syncer.supersede_metadata_bundle_if_needed") as mock_super:
        res_att = await syncer._ingest_item(505, "META_FAIL_EDGE", "Meta Edge Fail", force=False)
        assert res_att == "skipped"
        mock_super.assert_called_once_with("META_FAIL_EDGE", syncer)


def test_document_processor_metadata_edge_cases(tmp_path: Path):
    """Verify document processor edge cases: empty parent_item_key, pre-existing tmp_dir, failed ToC/relation generation."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    processor = DocumentProcessor(cache_dir)
    assert processor.has_child_attachments("") is False

    item = ZoteroItem(
        item_id=1,
        item_key="EDGE_ITEM",
        item_type="journalArticle",
        title="Edge Title",
        abstract="Some abstract text",
        creators=["Author X"],
        year=2025,
        publication=None,
        doi=None,
        url=None,
        tags=[],
        collections=[],
        relations=[],
    )
    syncer = DummySyncer(cache_dir)

    # Pre-create tmp_dir to trigger if tmp_dir.exists(): shutil.rmtree
    tmp_dir = cache_dir / ".tmp_EDGE_ITEM"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "stale.txt").write_text("stale", encoding="utf-8")

    # Fast path re-check test: call once, then call again with force=False
    b_dir1, k1 = materialize_item_metadata_bundle(item, syncer)
    assert b_dir1.exists()
    assert (b_dir1 / "document.md").exists()

    b_dir2, k2 = materialize_item_metadata_bundle(item, syncer, force=False)
    assert b_dir1 == b_dir2
    assert k1 == k2

    # Exceptions during keyword extraction, ToC, and item_relations generation
    with patch("zocrates.rag.keyword_extractor.extract_statistical_keywords", side_effect=Exception("KW fail")):
        with patch("zocrates.rag.toc_generator.generate_toc", side_effect=Exception("ToC fail")):
            with patch("zocrates.rag.bundle_catalog.regenerate_item_relations_md", side_effect=Exception("Relations fail")):
                b_dir3, _ = materialize_item_metadata_bundle(item, syncer, force=True)
                assert (b_dir3 / "document.md").exists()


def test_sync_shared_tags_target_key_and_primary_fallback(tmp_path: Path):
    """Verify sync_shared_tags with target_key, empty src_tags, and fallback to primary attachment key for tags."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "zotero.sqlite").touch()

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, parent_item_key, content_type, converter_id, converter_version, source_path, source_hash, converted_at, char_count)
            VALUES ('ATT_KEY', 'PARENT_P1', 'application/pdf', 'markitdown', '1.0', 'storage/ATT_KEY/doc.pdf', 'h1', '2026-01-01', 100),
                   ('ITEM_B', NULL, 'text/zotero-metadata', 'zotero_metadata', '1.0', 'zotero://item/ITEM_B', 'h2', '2026-01-01', 100)
            """
        )

        def mock_get_tags(zot_dir, item_key, tag_type=None):
            # PARENT_P1 has no tags directly, but ATT_KEY has tags
            if item_key == "ATT_KEY":
                return ["shared_tag_1", "shared_tag_2"]
            if item_key == "ITEM_B":
                return ["shared_tag_1", "shared_tag_2"]
            return []

        with patch("zocrates.rag.graph_engine.get_item_tags", side_effect=mock_get_tags):
            with patch("zocrates.rag.graph_engine.get_graph_relations_config") as mock_cfg:
                mock_cfg.return_value = {
                    "enabled": True,
                    "include_automatic_tags_in_shared_tags": True,
                    "min_shared_tags": 2,
                    "shared_tags_score": 0.70,
                }
                # 1. target_key is parent key PARENT_P1 -> resolves to ATT_KEY
                n = sync_shared_tags(conn=conn, zotero_dir=tmp_path, target_key="PARENT_P1", min_shared_tags=2)
                assert n >= 1

                # 2. target_key with no tags -> returns 0
                n_zero = sync_shared_tags(conn=conn, zotero_dir=tmp_path, target_key="NON_EXISTENT", min_shared_tags=2)
                assert n_zero == 0


def test_evaluate_and_persist_edges_abstract_less_ground_truth(tmp_path: Path):
    """Verify evaluate_and_persist_edges for abstract-less metadata item with ground truth and bundle files."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    item_dir = cache_dir / "NO_ABS"
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "document.md").write_text("# Doc\n\nNo abstract", encoding="utf-8")

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (item_key, title, content_type, converter_id, converter_version, source_path, source_hash, converted_at, char_count, abstract, abstract_source)
            VALUES ('NO_ABS', 'No Abstract Item', 'text/zotero-metadata', 'zotero_metadata', '1.0', 'zotero://item/NO_ABS', 'h_no_abs', '2026-01-01', 50, NULL, 'none')
            """
        )

    syncer = DummySyncer(cache_dir)
    syncer.store = MagicMock()

    with patch("zocrates.rag.graph_engine.get_graph_relations_config") as mock_cfg:
        mock_cfg.return_value = {
            "enabled": True,
            "include_human_curated": True,
            "include_shared_tags": True,
            "min_shared_tags": 2,
            "sync_bundle_files": True,
        }
        with patch("zocrates.rag.graph_engine.sync_manual_relations", return_value=0) as mock_man:
            with patch("zocrates.rag.graph_engine.sync_shared_tags", return_value=0) as mock_tags:
                res = evaluate_and_persist_edges("NO_ABS", syncer, sync_bundle_files=True, sync_ground_truth=True)
                mock_man.assert_called_once()
                mock_tags.assert_called_once()
                assert (item_dir / "item_relations.md").exists()

        # Exception during regenerate_item_relations_md
        with patch("zocrates.rag.bundle_catalog.regenerate_item_relations_md", side_effect=Exception("Regen fail")):
            res_fail = evaluate_and_persist_edges("NO_ABS", syncer, sync_bundle_files=True, sync_ground_truth=False)
            assert isinstance(res_fail, list)


@pytest.mark.asyncio
async def test_library_syncer_ingest_item_and_standalone_attachment(tmp_path: Path):
    """Verify LibrarySyncer._ingest_item for metadata item and _ingest_standalone_attachment supersession."""
    cache_dir = tmp_path / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. Empty parent_key in supersede_metadata_bundle_if_needed
    supersede_metadata_bundle_if_needed("", None)

    # 2. _ingest_item for metadata-only item (with abstract)
    syncer = LibrarySyncer.__new__(LibrarySyncer)
    syncer._zotero_data_dir = tmp_path
    syncer.processor = DocumentProcessor(cache_dir)
    syncer.store = MagicMock()
    syncer.store.is_ingested.return_value = False
    syncer.db = MagicMock()

    # Abstract-less item to test chunk_type = "metadata"
    item_no_abs = ZoteroItem(
        item_id=100,
        item_key="META_NO_ABS",
        item_type="journalArticle",
        title="No Abstract Item",
        abstract=None,
        creators=["Author Z"],
        year=2024,
        publication=None,
        doi=None,
        url=None,
        tags=[],
        collections=[],
        relations=[],
    )
    syncer.db.get_item.return_value = item_no_abs
    syncer.db.get_attachments.return_value = []
    syncer.db.get_notes_for_item.return_value = []
    res_no_abs = await syncer._ingest_item(100, "META_NO_ABS", "No Abstract Item", force=True, evaluate_edges=False)
    assert res_no_abs == "processed"
    chunks_passed = syncer.store.ingest_document.call_args[0][2]
    non_fm_chunks = [c for c in chunks_passed if c.chunk_type != "frontmatter"]
    assert len(non_fm_chunks) > 0
    assert all(c.chunk_type == "metadata" for c in non_fm_chunks)

    # Abstract-bearing item
    item = ZoteroItem(
        item_id=101,
        item_key="META_101",
        item_type="journalArticle",
        title="Meta Item Title",
        abstract="Substantive abstract here.",
        creators=["Author A"],
        year=2024,
        publication=None,
        doi=None,
        url=None,
        tags=[],
        collections=[],
        relations=[],
    )
    syncer.db.get_item.return_value = item
    res = await syncer._ingest_item(101, "META_101", "Meta Item Title", force=False, evaluate_edges=True)
    assert res == "processed"
    syncer.store.ingest_document.assert_called()
    assert (cache_dir / "META_101" / "document.md").exists()

    # 3. Call again when already fresh -> metadata_already_fresh
    syncer.store.is_ingested.return_value = True
    # Ensure indexed_at is in metadata
    syncer.processor.update_indexed_at("META_101")
    syncer.store.ingest_document.reset_mock()
    res2 = await syncer._ingest_item(101, "META_101", "Meta Item Title", force=False, evaluate_edges=False)
    assert res2 == "skipped"
    syncer.store.ingest_document.assert_not_called()

    # 4. _ingest_standalone_attachment supersedes metadata bundle
    att_file = tmp_path / "doc2.pdf"
    att_file.write_bytes(b"%PDF-1.4 file")
    from zocrates.rag.zotero_db import ZoteroAttachment
    attachment = ZoteroAttachment(
        item_id=202,
        item_key="ATT_202",
        parent_item_id=101,
        parent_item_key="META_101",
        content_type="application/pdf",
        path=att_file,
    )
    with patch("zocrates.rag.library_syncer.supersede_metadata_bundle_if_needed") as mock_super:
        with patch.object(syncer.processor, "get_or_convert", return_value="converted markdown"):
            res_att = await syncer._ingest_standalone_attachment(attachment, force=True)
            assert res_att == "processed"
            mock_super.assert_called_once_with("META_101", syncer)


