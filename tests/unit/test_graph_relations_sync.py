"""Unit tests for Graph Paper Relations: Synchronization & Tooling.

Tests:
  - ZoteroDB relations extraction (_fetch_relations, get_item_relations, get_all_item_relations, _build_item).
  - ZoteroDB tag extraction with tag_type filtering and parent fallback.
  - ZoteroDB recursive subcollection resolution via WITH RECURSIVE.
  - ZoteroDB fallback to flat collection resolution when parentCollectionID column is missing.
  - library_syncer incremental edge evaluation hooks in _ingest_item, child notes, standalone notes, standalone attachments.
  - library_syncer error isolation ensuring edge evaluation failures do not interrupt ingestion.
  - library_syncer full_sync relations and shared tags synchronization pass.
  - scripts/refresh_frontmatter.py --refresh-relations CLI functionality and keyword backfilling.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zocrates.rag.document_processor import _open_meta_db
from zocrates.rag.library_syncer import LibrarySyncer
from zocrates.rag.zotero_db import (
    ZoteroAttachment,
    ZoteroCollection,
    ZoteroDB,
    ZoteroItem,
    ZoteroNote,
)
from scripts.refresh_frontmatter import main, refresh_relations


# ---------------------------------------------------------------------------
# Test Helpers & Schemas
# ---------------------------------------------------------------------------

_ZOTERO_BASE_SCHEMA = """
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY,
    itemTypeID INTEGER DEFAULT 1,
    key TEXT NOT NULL
);
CREATE TABLE itemTypes (
    itemTypeID INTEGER PRIMARY KEY,
    typeName TEXT NOT NULL
);
CREATE TABLE deletedItems (
    itemID INTEGER PRIMARY KEY
);
CREATE TABLE itemData (
    itemID INTEGER,
    fieldID INTEGER,
    valueID INTEGER
);
CREATE TABLE fields (
    fieldID INTEGER PRIMARY KEY,
    fieldName TEXT NOT NULL
);
CREATE TABLE itemDataValues (
    valueID INTEGER PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE itemCreators (
    itemID INTEGER,
    creatorID INTEGER,
    orderIndex INTEGER
);
CREATE TABLE creators (
    creatorID INTEGER PRIMARY KEY,
    firstName TEXT,
    lastName TEXT
);
CREATE TABLE itemTags (
    itemID INTEGER,
    tagID INTEGER,
    type INTEGER DEFAULT 0
);
CREATE TABLE tags (
    tagID INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE collectionItems (
    collectionID INTEGER,
    itemID INTEGER
);
CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY,
    parentItemID INTEGER,
    contentType TEXT,
    path TEXT
);
CREATE TABLE itemNotes (
    itemID INTEGER PRIMARY KEY,
    parentItemID INTEGER,
    note TEXT,
    title TEXT
);
CREATE TABLE itemRelations (
    itemID INTEGER,
    predicateID INTEGER DEFAULT 1,
    object TEXT
);
"""


def _create_mock_zotero_sqlite(db_path: Path, with_parent_col: bool = True) -> None:
    """Create a populated mock Zotero SQLite database."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_ZOTERO_BASE_SCHEMA)

        if with_parent_col:
            conn.execute(
                """
                CREATE TABLE collections (
                    collectionID INTEGER PRIMARY KEY,
                    collectionName TEXT NOT NULL,
                    key TEXT NOT NULL,
                    parentCollectionID INTEGER
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE collections (
                    collectionID INTEGER PRIMARY KEY,
                    collectionName TEXT NOT NULL,
                    key TEXT NOT NULL
                )
                """
            )

        # Basic item types
        conn.execute("INSERT INTO itemTypes VALUES (1, 'journalArticle')")
        conn.execute("INSERT INTO itemTypes VALUES (2, 'attachment')")
        conn.execute("INSERT INTO itemTypes VALUES (3, 'note')")

        # Basic fields
        conn.execute("INSERT INTO fields VALUES (1, 'title')")
        conn.commit()


# ---------------------------------------------------------------------------
# 1. ZoteroDB Relations Extraction
# ---------------------------------------------------------------------------


def test_zotero_db_relations_extraction(tmp_path: Path):
    """Verify _fetch_relations, get_item_relations, and get_all_item_relations."""
    db_file = tmp_path / "zotero.sqlite"
    _create_mock_zotero_sqlite(db_file)

    with sqlite3.connect(str(db_file)) as conn:
        # Items: 1=KEYA, 2=KEYB, 3=KEYC, 4=KEYD (deleted)
        conn.execute("INSERT INTO items VALUES (1, 1, 'KEYA')")
        conn.execute("INSERT INTO items VALUES (2, 1, 'KEYB')")
        conn.execute("INSERT INTO items VALUES (3, 1, 'KEYC')")
        conn.execute("INSERT INTO items VALUES (4, 1, 'KEYD')")
        conn.execute("INSERT INTO deletedItems VALUES (4)")

        # Relations:
        # 1 (KEYA) -> KEYB
        # 1 (KEYA) -> KEYD (deleted target, should be excluded)
        # 2 (KEYB) -> KEYC
        # 4 (KEYD) -> KEYA (deleted source, should be excluded)
        conn.execute("INSERT INTO itemRelations VALUES (1, 1, 'http://zotero.org/users/0/items/KEYB')")
        conn.execute("INSERT INTO itemRelations VALUES (1, 1, 'http://zotero.org/users/0/items/KEYD')")
        conn.execute("INSERT INTO itemRelations VALUES (2, 1, 'http://zotero.org/users/local/123/items/KEYC')")
        conn.execute("INSERT INTO itemRelations VALUES (4, 1, 'http://zotero.org/users/0/items/KEYA')")
        conn.commit()

    zdb = ZoteroDB(sqlite_path=db_file, storage_dir=tmp_path / "storage")

    # Direct private helper test
    with sqlite3.connect(str(db_file)) as conn:
        conn.row_factory = sqlite3.Row
        rel_1 = zdb._fetch_relations(conn, 1)
        assert rel_1 == ["KEYB"]
        rel_4 = zdb._fetch_relations(conn, 4)
        assert rel_4 == []

    # Public method: get_item_relations by key and by id
    assert zdb.get_item_relations("KEYA") == ["KEYB"]
    assert zdb.get_item_relations(1) == ["KEYB"]
    assert zdb.get_item_relations("KEYB") == ["KEYC"]
    assert zdb.get_item_relations(2) == ["KEYC"]
    assert zdb.get_item_relations("KEYC") == []
    assert zdb.get_item_relations("KEYD") == []  # deleted
    assert zdb.get_item_relations(4) == []  # deleted
    assert zdb.get_item_relations("NONEXISTENT") == []

    # Public method: get_all_item_relations
    all_rels = zdb.get_all_item_relations()
    assert all_rels == [("KEYA", "KEYB"), ("KEYB", "KEYC")]

    # Verification of item population via _build_item in get_item and get_all_items
    item_a = zdb.get_item_by_key("KEYA")
    assert item_a is not None
    assert item_a.relations == ["KEYB"]

    items = zdb.get_all_items()
    items_by_key = {item.item_key: item for item in items}
    assert items_by_key["KEYA"].relations == ["KEYB"]
    assert items_by_key["KEYB"].relations == ["KEYC"]
    assert items_by_key["KEYC"].relations == []
    assert "KEYD" not in items_by_key


def test_zotero_db_get_item_tags(tmp_path: Path):
    """Verify get_item_tags with tag_type filtering and parent fallback."""
    db_file = tmp_path / "zotero.sqlite"
    _create_mock_zotero_sqlite(db_file)

    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("INSERT INTO items VALUES (1, 1, 'KEYA')")
        conn.execute("INSERT INTO items VALUES (2, 2, 'ATT1')")
        conn.execute("INSERT INTO items VALUES (3, 1, 'KEY_DEL')")
        conn.execute("INSERT INTO deletedItems VALUES (3)")

        # Attachment relationship: ATT1 is child of KEYA
        conn.execute("INSERT INTO itemAttachments VALUES (2, 1, 'application/pdf', 'file.pdf')")

        # Tags
        conn.execute("INSERT INTO tags VALUES (10, 'reinforcement-learning')")
        conn.execute("INSERT INTO tags VALUES (20, 'neural-networks')")
        conn.execute("INSERT INTO tags VALUES (30, 'auto-imported')")

        # KEYA has manual tags and auto tag
        conn.execute("INSERT INTO itemTags VALUES (1, 10, 0)")  # type 0 (user)
        conn.execute("INSERT INTO itemTags VALUES (1, 20, 0)")  # type 0 (user)
        conn.execute("INSERT INTO itemTags VALUES (1, 30, 1)")  # type 1 (auto)

        conn.commit()

    zdb = ZoteroDB(sqlite_path=db_file, storage_dir=tmp_path / "storage")

    # Fetch all tags
    all_tags = zdb.get_item_tags("KEYA")
    assert sorted(all_tags) == ["auto-imported", "neural-networks", "reinforcement-learning"]

    # Filter by user tags (tag_type=0)
    user_tags = zdb.get_item_tags("KEYA", tag_type=0)
    assert sorted(user_tags) == ["neural-networks", "reinforcement-learning"]

    # Filter by auto tags (tag_type=1)
    auto_tags = zdb.get_item_tags("KEYA", tag_type=1)
    assert auto_tags == ["auto-imported"]

    # Numeric ID query
    assert sorted(zdb.get_item_tags(1, tag_type=0)) == ["neural-networks", "reinforcement-learning"]

    # Attachment fallback: ATT1 has no direct tags, inherits from parent KEYA (id 1)
    att_tags = zdb.get_item_tags("ATT1", tag_type=0)
    assert sorted(att_tags) == ["neural-networks", "reinforcement-learning"]

    # Deleted item returns empty
    assert zdb.get_item_tags("KEY_DEL") == []


# ---------------------------------------------------------------------------
# 2. ZoteroDB Recursive Subcollections
# ---------------------------------------------------------------------------


def test_zotero_db_recursive_subcollections(tmp_path: Path):
    """Verify recursive collection hierarchy traversal with WITH RECURSIVE."""
    db_file = tmp_path / "zotero.sqlite"
    _create_mock_zotero_sqlite(db_file, with_parent_col=True)

    with sqlite3.connect(str(db_file)) as conn:
        # Hierarchy:
        # Root (1: "AI", key: "COL_AI", parent: NULL)
        #   -> Child (2: "Machine Learning", key: "COL_ML", parent: 1)
        #        -> Grandchild (3: "Transformers", key: "COL_TR", parent: 2)
        # Sibling (4: "Neuroscience", key: "COL_NEURO", parent: NULL)
        conn.execute("INSERT INTO collections VALUES (1, 'AI', 'COL_AI', NULL)")
        conn.execute("INSERT INTO collections VALUES (2, 'Machine Learning', 'COL_ML', 1)")
        conn.execute("INSERT INTO collections VALUES (3, 'Transformers', 'COL_TR', 2)")
        conn.execute("INSERT INTO collections VALUES (4, 'Neuroscience', 'COL_NEURO', NULL)")

        # Items
        conn.execute("INSERT INTO items VALUES (10, 1, 'KEY_AI')")
        conn.execute("INSERT INTO items VALUES (20, 1, 'KEY_ML')")
        conn.execute("INSERT INTO items VALUES (30, 1, 'KEY_TR')")
        conn.execute("INSERT INTO items VALUES (40, 1, 'KEY_NEURO')")
        conn.execute("INSERT INTO items VALUES (50, 1, 'KEY_DEL')")
        conn.execute("INSERT INTO deletedItems VALUES (50)")

        # Assignments
        conn.execute("INSERT INTO collectionItems VALUES (1, 10)")
        conn.execute("INSERT INTO collectionItems VALUES (2, 20)")
        conn.execute("INSERT INTO collectionItems VALUES (3, 30)")
        conn.execute("INSERT INTO collectionItems VALUES (3, 50)")  # deleted
        conn.execute("INSERT INTO collectionItems VALUES (4, 40)")
        conn.commit()

    zdb = ZoteroDB(sqlite_path=db_file, storage_dir=tmp_path / "storage")

    # Recursive traversal from root: should include KEY_AI, KEY_ML, KEY_TR (excluding KEY_DEL and KEY_NEURO)
    ai_keys_recursive = zdb.get_item_keys_in_collection("AI", recursive=True)
    assert set(ai_keys_recursive) == {"KEY_AI", "KEY_ML", "KEY_TR"}

    # Query by key
    ai_keys_by_key = zdb.get_item_keys_in_collection("COL_AI", recursive=True)
    assert set(ai_keys_by_key) == {"KEY_AI", "KEY_ML", "KEY_TR"}

    # Flat traversal from root: only direct items
    ai_keys_flat = zdb.get_item_keys_in_collection("AI", recursive=False)
    assert ai_keys_flat == ["KEY_AI"]

    # Mid-level subcollection recursive traversal
    ml_keys_recursive = zdb.get_item_keys_in_collection("Machine Learning", recursive=True)
    assert set(ml_keys_recursive) == {"KEY_ML", "KEY_TR"}

    # Leaf subcollection
    tr_keys = zdb.get_item_keys_in_collection("Transformers", recursive=True)
    assert tr_keys == ["KEY_TR"]


def test_zotero_db_flat_fallback_without_parent_column(tmp_path: Path):
    """Verify graceful fallback to flat query when parentCollectionID column is missing."""
    db_file = tmp_path / "zotero.sqlite"
    _create_mock_zotero_sqlite(db_file, with_parent_col=False)

    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("INSERT INTO collections VALUES (1, 'AI', 'COL_AI')")
        conn.execute("INSERT INTO items VALUES (10, 1, 'KEY1')")
        conn.execute("INSERT INTO collectionItems VALUES (1, 10)")
        conn.commit()

    zdb = ZoteroDB(sqlite_path=db_file, storage_dir=tmp_path / "storage")

    # Even with recursive=True, because parentCollectionID does not exist, executes flat query
    keys = zdb.get_item_keys_in_collection("AI", recursive=True)
    assert keys == ["KEY1"]


# ---------------------------------------------------------------------------
# 3. LibrarySyncer Incremental Edge Hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_library_syncer_incremental_edge_hooks(tmp_path: Path):
    """Verify incremental evaluate_and_persist_edges invocations during ingestion."""
    mock_db = MagicMock(spec=ZoteroDB)
    mock_store = MagicMock()
    mock_store.is_ingested.return_value = False
    mock_processor = MagicMock()
    mock_processor._cache_dir = tmp_path / "cache"
    mock_processor._cache_dir.mkdir(parents=True, exist_ok=True)
    mock_processor.get_or_convert.return_value = "# Header\nSome body text for indexing."
    mock_processor.get_metadata.return_value = {"title": "Doc"}

    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    sqlite_file.touch()

    syncer = LibrarySyncer(zotero_data_dir=zotero_dir, sqlite_path=sqlite_file)
    syncer.db = mock_db
    syncer.store = mock_store
    syncer.processor = mock_processor

    dummy_pdf = tmp_path / "sample.pdf"
    dummy_pdf.touch()

    att = ZoteroAttachment(
        item_id=101,
        item_key="ATT1",
        parent_item_id=1,
        parent_item_key="PARENT1",
        content_type="application/pdf",
        path=dummy_pdf,
        title="Sample Attachment",
    )
    note = ZoteroNote(
        note_id=201,
        note_key="NOTE1",
        parent_item_id=1,
        parent_item_key="PARENT1",
        title="Child Note Title",
        note_html="<p>Child note body text</p>",
    )

    mock_db.get_attachments.return_value = [att]
    mock_db.get_notes_for_item.return_value = [note]
    mock_db.get_item.return_value = ZoteroItem(
        item_id=1,
        item_key="PARENT1",
        item_type="journalArticle",
        title="Parent Work Title",
    )

    with (
        patch("zocrates.rag.library_syncer.materialize_note_bundle"),
        patch("zocrates.rag.graph_engine.evaluate_and_persist_edges") as mock_evaluate,
    ):
        res = await syncer._ingest_item(1, "PARENT1", "Parent Work Title", force=True)
        assert res == "processed"

        # Both the attachment and the child note must have triggered evaluate_and_persist_edges
        mock_evaluate.assert_any_call("ATT1", syncer, True)
        mock_evaluate.assert_any_call("NOTE1", syncer, True)


@pytest.mark.asyncio
async def test_library_syncer_standalone_hooks_and_error_isolation(tmp_path: Path):
    """Verify standalone notes/attachments trigger hooks and exceptions are gracefully caught."""
    mock_db = MagicMock(spec=ZoteroDB)
    mock_store = MagicMock()
    mock_store.is_ingested.return_value = False
    mock_processor = MagicMock()
    mock_processor._cache_dir = tmp_path / "cache"
    mock_processor._cache_dir.mkdir(parents=True, exist_ok=True)
    mock_processor.get_or_convert.return_value = "# Standalone\nText content."
    mock_processor.get_metadata.return_value = {}

    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    sqlite_file.touch()

    syncer = LibrarySyncer(zotero_data_dir=zotero_dir, sqlite_path=sqlite_file)
    syncer.db = mock_db
    syncer.store = mock_store
    syncer.processor = mock_processor

    standalone_note = ZoteroNote(
        note_id=301,
        note_key="ST_NOTE",
        parent_item_id=None,
        parent_item_key=None,
        title="Standalone Note",
        note_html="<p>Important thoughts</p>",
    )
    dummy_pdf = tmp_path / "standalone.pdf"
    dummy_pdf.touch()
    standalone_att = ZoteroAttachment(
        item_id=401,
        item_key="ST_ATT",
        parent_item_id=None,
        parent_item_key=None,
        content_type="application/pdf",
        path=dummy_pdf,
        title="Standalone File",
    )

    with (
        patch("zocrates.rag.library_syncer.materialize_note_bundle"),
        patch("zocrates.rag.graph_engine.evaluate_and_persist_edges") as mock_evaluate,
    ):
        # 1. Standalone note success
        res_note = await syncer._ingest_standalone_note(standalone_note, force=True)
        assert res_note == "processed"
        mock_evaluate.assert_called_with("ST_NOTE", syncer, True)

        # 2. Standalone attachment success
        res_att = await syncer._ingest_standalone_attachment(standalone_att, force=True)
        assert res_att == "processed"
        mock_evaluate.assert_called_with("ST_ATT", syncer, True)

        # 3. Polymorphic sync_item convenience method
        mock_db.get_item_by_key.return_value = None
        mock_db.get_note_by_key.return_value = None
        mock_db.get_attachment_by_key.return_value = standalone_att
        res_sync_item = await syncer.sync_item("ST_ATT", force=True)
        assert res_sync_item == "processed"

        # 3b. Polymorphic sync_item by integer item_id
        mock_db.get_item.return_value = None
        mock_db.get_note_by_id.return_value = None
        mock_db.get_attachment_by_id.return_value = standalone_att
        res_sync_item_id = await syncer.sync_item(1001, force=True)
        assert res_sync_item_id == "processed"

        # 4. Resilience check: evaluate_and_persist_edges raises exception
        mock_evaluate.side_effect = RuntimeError("Simulated edge evaluation failure")
        res_resilient = await syncer._ingest_standalone_note(standalone_note, force=True)
        # Should not raise exception and should still return 'processed'
        assert res_resilient == "processed"

        # 4b. Attachment edge failure resilience
        mock_db.get_attachments.return_value = [standalone_att]
        mock_db.get_notes_for_item.return_value = []
        syncer.store.is_ingested.return_value = False
        syncer.processor.get_or_convert = MagicMock(
            return_value="# Doc content\n\n" + ("This is detailed paragraph text explaining concepts. " * 10)
        )
        res_item_resilient = await syncer._ingest_item(10, "ITEM1", "Title", force=True)
        assert res_item_resilient == "processed"


@pytest.mark.asyncio
async def test_library_syncer_full_sync_relation_passes(tmp_path: Path):
    """Verify full_sync runs sync_manual_relations and sync_shared_tags."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    sqlite_file.touch()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_db = cache_dir / "conversion_meta.db"

    # Initialize conversion_meta.db
    with _open_meta_db(cache_dir) as conn:
        conn.execute("SELECT 1")

    syncer = LibrarySyncer(zotero_data_dir=zotero_dir, sqlite_path=sqlite_file)
    syncer.processor._cache_dir = cache_dir
    syncer.processor.list_cached_keys = MagicMock(return_value=[])
    syncer.processor.regenerate_root_index = MagicMock()
    syncer.sync_metadata_staleness = MagicMock(return_value={"updated": 0})

    mock_db = MagicMock(spec=ZoteroDB)
    mock_db.get_all_items.return_value = []
    mock_db.get_standalone_notes.return_value = []
    mock_db.get_standalone_attachments.return_value = []
    mock_db.get_all_attachments.return_value = []
    syncer.db = mock_db

    with (
        patch("zocrates.rag.graph_engine.sync_manual_relations") as mock_sync_manual,
        patch("zocrates.rag.graph_engine.sync_shared_tags") as mock_sync_tags,
    ):
        summary = await syncer.full_sync()
        assert summary["total"] == 0
        mock_sync_manual.assert_called_once()
        mock_sync_tags.assert_called_once()


@pytest.mark.asyncio
async def test_library_syncer_full_sync_unmocked_persistence(tmp_path: Path):
    """Verify full_sync runs real unmocked sync_manual_relations across threads without SQLite errors."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    _create_mock_zotero_sqlite(sqlite_file)

    with sqlite3.connect(str(sqlite_file)) as z_conn:
        z_conn.execute("INSERT INTO items VALUES (1, 1, 'ITEM1')")
        z_conn.execute("INSERT INTO items VALUES (2, 1, 'ITEM2')")
        z_conn.execute("INSERT INTO itemRelations VALUES (1, 1, 'http://zotero.org/users/0/items/ITEM2')")
        z_conn.commit()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Initialize conversion_meta.db with test items
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (
                item_key, title, converter_id, converter_version,
                source_path, content_type, converted_at, source_hash,
                char_count, abstract, keywords
            )
            VALUES ('ITEM1', 'Paper 1', 'markitdown', '1.0', 'p1', 'application/pdf', 'now', 'h1', 100, 'abs', '["ai"]')
            """
        )
        conn.execute(
            """
            INSERT INTO conversion_meta (
                item_key, title, converter_id, converter_version,
                source_path, content_type, converted_at, source_hash,
                char_count, abstract, keywords
            )
            VALUES ('ITEM2', 'Paper 2', 'markitdown', '1.0', 'p2', 'application/pdf', 'now', 'h2', 100, 'abs', '["ai"]')
            """
        )
        conn.commit()

    syncer = LibrarySyncer(zotero_data_dir=zotero_dir, sqlite_path=sqlite_file)
    syncer.processor._cache_dir = cache_dir
    syncer.processor.list_cached_keys = MagicMock(return_value=["ITEM1", "ITEM2"])
    syncer.processor.regenerate_root_index = MagicMock()
    syncer.sync_metadata_staleness = MagicMock(return_value={"updated": 0})

    mock_db = MagicMock(spec=ZoteroDB)
    mock_db.get_all_items.return_value = [
        ZoteroItem(1, "ITEM1", "journalArticle", "Paper 1"),
        ZoteroItem(2, "ITEM2", "journalArticle", "Paper 2"),
    ]
    mock_db.get_attachments.return_value = []
    mock_db.get_notes_for_item.return_value = []
    mock_db.get_standalone_notes.return_value = []
    mock_db.get_standalone_attachments.return_value = []
    mock_db.get_all_attachments.return_value = []
    syncer.db = mock_db

    # Execute real full_sync without mocking sync_manual_relations or sync_shared_tags
    summary = await syncer.full_sync()
    assert summary["total"] == 2

    # Verify that the manual relation from mock zotero.sqlite (ITEM1 -> ITEM2) was persisted to conversion_meta.db!
    with _open_meta_db(cache_dir) as conn:
        edges = conn.execute(
            "SELECT source_key, target_key, relation_type, score FROM item_relations WHERE source_key = 'ITEM1'"
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["target_key"] == "ITEM2"
        assert edges[0]["relation_type"] == "human_curated"
        assert edges[0]["score"] == 1.0


# ---------------------------------------------------------------------------
# 4. scripts/refresh_frontmatter.py --refresh-relations CLI functionality
# ---------------------------------------------------------------------------


def test_refresh_frontmatter_cli_refresh_relations(tmp_path: Path):
    """Verify CLI --refresh-relations runs ground truth sync, extracts missing keywords, and updates bundles."""
    zotero_dir = tmp_path / "Zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    _create_mock_zotero_sqlite(sqlite_file)

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    cache_dir.mkdir(parents=True, exist_ok=True)

    bundle_dir = cache_dir / "ITEM1"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "document.md").write_text(
        "---\ntitle: Attention Is All You Need\n---\n# Abstract\nTransformers replace recurrence with self-attention.",
        encoding="utf-8",
    )

    # Initialize conversion_meta.db with missing keywords
    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            """
            INSERT INTO conversion_meta (
                item_key, title, converter_id, converter_version,
                source_path, content_type, converted_at, source_hash,
                char_count, abstract, keywords
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ITEM1",
                "Attention Is All You Need",
                "markitdown",
                "1.0",
                "storage/ITEM1/sample.pdf",
                "application/pdf",
                "2026-08-20T12:00:00Z",
                "hash123",
                1000,
                "Transformers replace recurrence with self-attention.",
                None,  # Missing keywords
            ),
        )
        conn.commit()

    with (
        patch("zocrates.rag.graph_engine.sync_manual_relations") as mock_sync_manual,
        patch("zocrates.rag.graph_engine.sync_shared_tags") as mock_sync_tags,
        patch("zocrates.rag.graph_engine.evaluate_and_persist_edges") as mock_eval,
    ):
        mock_eval.return_value = []

        # Run CLI main with --refresh-relations
        main(["--data-dir", str(zotero_dir), "--refresh-relations", "ITEM1"])

        mock_sync_manual.assert_called_once()
        mock_sync_tags.assert_called_once()

    # Verify keywords were extracted and persisted to conversion_meta.db
    with _open_meta_db(cache_dir) as conn:
        row = conn.execute("SELECT keywords FROM conversion_meta WHERE item_key = 'ITEM1'").fetchone()
        assert row is not None
        assert row["keywords"] is not None
        kw_list = json.loads(row["keywords"])
        assert isinstance(kw_list, list)
        assert len(kw_list) > 0

    # Verify item_relations.md was generated (0-byte orphan since no edges) and index.md cataloged it
    rel_file = bundle_dir / "item_relations.md"
    assert rel_file.exists()
    index_file = bundle_dir / "index.md"
    assert index_file.exists()
    assert "item_relations.md" in index_file.read_text(encoding="utf-8")
