"""Unit tests for the Zotero SQLite database parser."""

from contextlib import contextmanager
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from zocrates.rag.zotero_db import (
    ZoteroAttachment,
    ZoteroCollection,
    ZoteroDB,
    ZoteroNote,
    _cleanup_sqlite_snapshots,
    _open_db,
    _open_snapshot_connection,
    _sqlite_snapshots,
    extract_year,
    normalize_doi,
)

# Simple schema matching the subset of tables we query
_ZOTERO_TEST_SCHEMA = """
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, key TEXT);
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, contentType TEXT, path TEXT);
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, note TEXT, title TEXT);
"""


@pytest.fixture
def temp_zotero_db(tmp_path) -> tuple[Path, Path]:
    """Create an in-memory SQLite database populated with minimal Zotero schema and test data.

    Inserts one journal article (ID 10), one PDF attachment (ID 11), one
    standalone note (ID 12), and one child note (ID 13) attached to item 10.

    Returns:
        Tuple of ``(db_path, storage_dir)`` pointing to the temporary SQLite
        file and storage directory.
    """
    db_path = tmp_path / "zotero.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA)

    # Insert standard types
    conn.execute(
        "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')"
    )
    conn.execute(
        "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (2, 'attachment')"
    )
    conn.execute(
        "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (3, 'note')"
    )

    # Insert fields
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'date')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (3, 'abstractNote')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (4, 'DOI')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (5, 'url')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (6, 'publicationTitle')")

    # Insert data values
    conn.execute(
        "INSERT INTO itemDataValues (valueID, value) VALUES (1, 'Awesome Paper Title')"
    )
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (2, '2025-05-12')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (3, 'An abstract')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (4, 'https://doi.org/10.1234/test')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (5, 'https://example.com/paper')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (6, 'Journal of AI Research')")

    # Insert an item (ID 10)
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, key) VALUES (10, 1, 'ITEM10AB')"
    )
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 1, 1)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 2, 2)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 3, 3)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 4, 4)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 5, 5)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 6, 6)")

    # Insert creator for item 10
    conn.execute(
        "INSERT INTO creators (creatorID, firstName, lastName) VALUES (100, 'Alex', 'Smith')"
    )
    conn.execute(
        "INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (10, 100, 0)"
    )

    # Insert collection for item 10
    conn.execute(
        "INSERT INTO collections (collectionID, collectionName, key) VALUES (200, 'Machine Learning', 'COLL200KEY')"
    )
    conn.execute("INSERT INTO collectionItems (collectionID, itemID) VALUES (200, 10)")

    # Insert attachment title value (ID 7 = "Full Text PDF")
    conn.execute(
        "INSERT INTO itemDataValues (valueID, value) VALUES (7, 'Full Text PDF')"
    )

    # Insert attachment (ID 11) pointing to parent item 10, with a title in itemData
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, key) VALUES (11, 2, 'ATTACH11')"
    )
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, path) VALUES (11, 10, 'application/pdf', 'storage:paper.pdf')"
    )
    conn.execute(
        "INSERT INTO itemData (itemID, fieldID, valueID) VALUES (11, 1, 7)"
    )

    # Insert a second attachment (ID 14) – HTML, no custom title (title falls back to filename)
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, key) VALUES (14, 2, 'ATTACH14')"
    )
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, path) VALUES (14, 10, 'text/html', 'storage:article.html')"
    )

    # Insert Standalone Note (ID 12)
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, key) VALUES (12, 3, 'NOTE12SA')"
    )
    conn.execute(
        "INSERT INTO itemNotes (itemID, parentItemID, note, title) VALUES (12, NULL, '<p>Standalone research note</p>', 'Standalone Note')"
    )

    # Insert Child Note (ID 13) attached to Item 10
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, key) VALUES (13, 3, 'NOTE13CH')"
    )
    conn.execute(
        "INSERT INTO itemNotes (itemID, parentItemID, note, title) VALUES (13, 10, '<h2>Summary</h2><p>Attached note summary</p>', 'Child Note Summary')"
    )

    conn.commit()
    conn.close()

    return db_path, storage_dir


def test_zotero_db_fetching(temp_zotero_db):
    """Verify that items, attachments, and full item metadata are correctly retrieved.

    Asserts:
      - ``get_all_items`` returns exactly the one non-attachment, non-note item.
      - ``get_item`` returns the correct title, year, creator, and collection.
      - ``get_attachments`` returns the PDF with the resolved storage path.
    """
    db_path, storage_dir = temp_zotero_db
    z_db = ZoteroDB(db_path, storage_dir)

    # Test get_all_items
    all_items = z_db.get_all_items()
    assert len(all_items) == 1
    assert all_items[0].item_id == 10
    assert all_items[0].title == "Awesome Paper Title"
    assert all_items[0].year == "2025"
    assert all_items[0].abstract == "An abstract"
    assert all_items[0].doi == "10.1234/test"
    assert all_items[0].url == "https://example.com/paper"
    assert all_items[0].publication == "Journal of AI Research"
    assert all_items[0].creators == ["Smith, Alex"]
    assert all_items[0].collections == [ZoteroCollection(name="Machine Learning", key="COLL200KEY")]

    # Test get_item details
    item = z_db.get_item(10)
    assert item is not None
    assert item.title == "Awesome Paper Title"
    assert item.year == "2025"
    assert item.doi == "10.1234/test"
    assert item.url == "https://example.com/paper"
    assert item.publication == "Journal of AI Research"
    assert item.creators == ["Smith, Alex"]
    assert item.collections == [ZoteroCollection(name="Machine Learning", key="COLL200KEY")]

    # Test get_attachments – two attachments now
    attachments = z_db.get_attachments(10)
    assert len(attachments) == 2

    pdf_att = next(a for a in attachments if a.item_key == "ATTACH11")
    assert pdf_att.item_id == 11
    assert pdf_att.parent_item_id == 10
    assert pdf_att.parent_item_key == "ITEM10AB"
    assert pdf_att.content_type == "application/pdf"
    assert pdf_att.path == storage_dir / "ATTACH11" / "paper.pdf"
    # Custom itemData title differs from filename → composite title expected
    assert pdf_att.title == "Full Text PDF (paper.pdf)"

    html_att = next(a for a in attachments if a.item_key == "ATTACH14")
    assert html_att.content_type == "text/html"
    # No itemData title → falls back to filename
    assert html_att.title == "article.html"


def test_zotero_db_note_fetching(temp_zotero_db):
    """Verify standalone and child note retrieval by parent item ID.

    Asserts:
      - ``get_all_notes`` returns both the standalone and child note.
      - ``get_standalone_notes`` returns only the note with no parent.
      - ``get_notes_for_item`` returns only the child note linked to item 10.
    """
    db_path, storage_dir = temp_zotero_db
    z_db = ZoteroDB(db_path, storage_dir)

    all_notes = z_db.get_all_notes()
    assert len(all_notes) == 2

    standalone = z_db.get_standalone_notes()
    assert len(standalone) == 1
    assert standalone[0].note_id == 12
    assert standalone[0].note_key == "NOTE12SA"
    assert standalone[0].title == "Standalone Note"
    assert "Standalone research note" in standalone[0].note_html

    child_notes = z_db.get_notes_for_item(10)
    assert len(child_notes) == 1
    assert child_notes[0].note_id == 13
    assert child_notes[0].note_key == "NOTE13CH"
    assert child_notes[0].parent_item_id == 10
    assert child_notes[0].parent_item_key == "ITEM10AB"
    assert child_notes[0].title == "Child Note Summary"
    assert "Attached note summary" in child_notes[0].note_html


def test_normalize_doi():
    """Verify DOI prefix stripping."""
    assert normalize_doi("https://doi.org/10.1038/s41586-020-2649-2") == "10.1038/s41586-020-2649-2"
    assert normalize_doi("http://doi.org/10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi("https://dx.doi.org/10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi("doi.org/10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi("DOI: 10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi("doi:10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi("10.1038/s41586") == "10.1038/s41586"
    assert normalize_doi(None) is None
    assert normalize_doi("") is None


def test_extract_year():
    """Verify 4-digit year extraction from various date formats."""
    assert extract_year("2021-01-00 01/2021") == "2021"
    assert extract_year("2005-06-00 2005-06") == "2005"
    assert extract_year("2025-02-28 2025-02-28") == "2025"
    assert extract_year("May 2020") == "2020"
    assert extract_year("20230101") == "2023"
    assert extract_year("1998") == "1998"
    assert extract_year(None) is None
    assert extract_year("") is None
    assert extract_year("Unknown Date") is None


def test_get_note_by_key(temp_zotero_db):
    """Verify get_note_by_key retrieves the correct note and returns None for missing keys."""
    db_path, storage_dir = temp_zotero_db
    z_db = ZoteroDB(db_path, storage_dir)

    # Existing child note
    note = z_db.get_note_by_key("NOTE13CH")
    assert note is not None
    assert isinstance(note, ZoteroNote)
    assert note.note_id == 13
    assert note.note_key == "NOTE13CH"
    assert note.parent_item_id == 10
    assert note.parent_item_key == "ITEM10AB"
    assert note.title == "Child Note Summary"
    assert "Attached note summary" in note.note_html

    # Standalone note is also retrievable by key
    standalone = z_db.get_note_by_key("NOTE12SA")
    assert standalone is not None
    assert standalone.note_key == "NOTE12SA"
    assert standalone.parent_item_id is None

    # Non-existent key returns None
    missing = z_db.get_note_by_key("DOESNOTEXIST")
    assert missing is None


def test_attachment_title_fallbacks(temp_zotero_db):
    """Verify ZoteroAttachment.title falls back to filename when no itemData title exists."""
    db_path, storage_dir = temp_zotero_db
    z_db = ZoteroDB(db_path, storage_dir)

    attachments = z_db.get_attachments(10)
    assert len(attachments) == 2

    titles = {a.item_key: a.title for a in attachments}
    # Custom title set via itemData; differs from filename → composite
    assert titles["ATTACH11"] == "Full Text PDF (paper.pdf)"
    # No itemData title → filename from path
    assert titles["ATTACH14"] == "article.html"


def test_publication_fallback_priority(tmp_path):
    """Verify that ZoteroDB prioritizes publicationTitle, proceedingsTitle, repository, etc."""
    db_path = tmp_path / "zotero_pub.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA)

    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'preprint')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'repository')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (3, 'publisher')")

    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (1, 'Preprint Title')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (2, 'arXiv')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (3, 'Fallback Publisher')")

    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (50, 1, 'PREPRINT1')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (50, 1, 1)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (50, 2, 2)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (50, 3, 3)")

    conn.commit()
    conn.close()

    z_db = ZoteroDB(db_path, storage_dir)
    item = z_db.get_item(50)
    assert item is not None
    # repository has higher priority than publisher in _PUBLICATION_FIELDS
    assert item.publication == "arXiv"


def test_get_active_item_keys_and_is_item_active(temp_zotero_db):
    """Verify get_active_item_keys and is_item_active filter out nonexistent, deleted, and parent-deleted items."""
    db_path, storage_dir = temp_zotero_db
    z_db = ZoteroDB(db_path, storage_dir)

    # Initial state: ITEM10AB, ATTACH11, NOTE12SA, NOTE13CH, ATTACH14 are all active
    active = z_db.get_active_item_keys(["ITEM10AB", "ATTACH11", "NOTE12SA", "NONEXISTENT"])
    assert active == {"ITEM10AB", "ATTACH11", "NOTE12SA"}
    assert z_db.is_item_active("ITEM10AB") is True
    assert z_db.is_item_active("ATTACH11") is True
    assert z_db.is_item_active("NONEXISTENT") is False

    # Directly delete standalone note (ID 12)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO deletedItems (itemID) VALUES (12)")
    conn.commit()
    conn.close()

    assert z_db.is_item_active("NOTE12SA") is False
    assert z_db.get_active_item_keys(["ITEM10AB", "ATTACH11", "NOTE12SA"]) == {"ITEM10AB", "ATTACH11"}

    # Now delete parent item (ID 10)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO deletedItems (itemID) VALUES (10)")
    conn.commit()
    conn.close()

    # Parent is deleted
    assert z_db.is_item_active("ITEM10AB") is False
    # Child attachment is inactive because its parent is in deletedItems
    assert z_db.is_item_active("ATTACH11") is False
    # Child note is inactive because its parent is in deletedItems
    assert z_db.is_item_active("NOTE13CH") is False
    assert z_db.get_active_item_keys(["ITEM10AB", "ATTACH11", "NOTE13CH"]) == set()


def test_get_all_dois_arxiv_ids_and_dedup_records(temp_zotero_db):
    """Verify get_all_dois, get_all_arxiv_ids, get_all_titles, and get_all_dedup_records."""
    db_path, storage_dir = temp_zotero_db

    # Insert a preprint item with arXiv ID in the extra field
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (7, 'extra')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (8, 'Preprint on Transformers')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (9, 'arXiv: 2305.12345v2 [cs.AI]')")

    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (20, 1, 'ITEM20PRE')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (20, 1, 8)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (20, 7, 9)")
    conn.commit()
    conn.close()

    z_db = ZoteroDB(db_path, storage_dir)

    # DOIs
    dois = z_db.get_all_dois()
    assert "10.1234/test" in dois

    # arXiv IDs
    arxiv_ids = z_db.get_all_arxiv_ids()
    assert "2305.12345" in arxiv_ids

    # Titles
    titles = z_db.get_all_titles()
    assert "Awesome Paper Title" in titles
    assert "Preprint on Transformers" in titles

    # Dedup records
    records = z_db.get_all_dedup_records()
    assert len(records) == 2
    rec_by_id = {r["item_id"]: r for r in records}

    rec10 = rec_by_id[10]
    assert rec10["title"] == "Awesome Paper Title"
    assert rec10["doi"] == "10.1234/test"
    assert rec10["year"] == 2025
    assert rec10["creators"] == ["Smith"]

    rec20 = rec_by_id[20]
    assert rec20["title"] == "Preprint on Transformers"
    assert rec20["arxiv_id"] == "2305.12345"

    # Also verify get_item extracts arxiv_id
    item20 = z_db.get_item(20)
    assert item20 is not None
    assert item20.arxiv_id == "2305.12345"




# ---------------------------------------------------------------------------
# WAL-correct reads (fresh-profile fix regression)
# ---------------------------------------------------------------------------


def test_open_db_reads_uncheckpointed_wal(tmp_path):
    """Tier 1 (mode=ro): committed WAL frames are visible (Zotero-running regime).

    Zotero writes into zotero.sqlite-wal and only checkpoints on shutdown; a
    read-only connection with a live WAL/SHM pair must see that data. The
    legacy immutable=1 strategy silently reported 0 items here.
    """
    db_path = tmp_path / "zotero.sqlite"
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(_ZOTERO_TEST_SCHEMA)
        writer.execute(
            "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')"
        )
        writer.execute("INSERT INTO items (itemTypeID, key) VALUES (1, 'WALITEM1')")
        writer.commit()
        # Writer stays open -> WAL persists and -shm exists.

        with _open_db(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            assert count == 1, "mode=ro connection must see uncheckpointed WAL data"
    finally:
        writer.close()


def test_snapshot_connection_recovers_hot_wal(tmp_path):
    """Tier 2: unclean shutdown (hot -wal, no -shm) recovers via snapshot copy.

    Replicates the observed fresh-profile failure: Zotero killed mid-session
    leaves an un-checkpointed -wal and no -shm, so mode=ro reports
    "database is locked" and immutable=1 silently reads the stale main file
    (0 items). The snapshot copy performs WAL recovery on a private copy.
    """
    _cleanup_sqlite_snapshots()
    src = tmp_path / "zotero.sqlite"
    writer = sqlite3.connect(str(src))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(_ZOTERO_TEST_SCHEMA)
        writer.execute(
            "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')"
        )
        writer.execute("INSERT INTO items (itemTypeID, key) VALUES (1, 'WALITEM2')")
        writer.commit()

        # Copy main + wal while the writer is open, then close all
        # connections: the source checkpoints cleanly, the copy keeps the
        # hot WAL (no -shm) — exactly the observed unclean state.
        dst_dir = tmp_path / "hot"
        dst_dir.mkdir()
        hot = dst_dir / "zotero.sqlite"
        shutil.copy2(src, hot)
        shutil.copy2(str(src) + "-wal", str(hot) + "-wal")
    finally:
        writer.close()
    assert not os.path.exists(str(hot) + "-shm")

    conn = _open_snapshot_connection(hot)
    try:
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count == 1, "snapshot copy must recover committed WAL frames"
    finally:
        conn.close()

    # Same signature -> cached snapshot reused (single cache entry).
    conn2 = _open_snapshot_connection(hot)
    try:
        assert conn2.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    finally:
        conn2.close()
    assert len(_sqlite_snapshots) == 1

    # Main-file mtime change -> signature invalidation -> snapshot rebuilt.
    os.utime(hot, None)
    conn3 = _open_snapshot_connection(hot)
    try:
        assert conn3.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    finally:
        conn3.close()
    assert len(_sqlite_snapshots) == 1  # replaced, not accumulated

    _cleanup_sqlite_snapshots()
    assert len(_sqlite_snapshots) == 0


def test_open_db_reuses_cached_snapshot(tmp_path):
    """_open_db checks _sqlite_snapshots first and reuses existing snapshot without mode=ro probe."""
    _cleanup_sqlite_snapshots()
    src = tmp_path / "zotero.sqlite"
    writer = sqlite3.connect(str(src))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(_ZOTERO_TEST_SCHEMA)
        writer.execute(
            "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')"
        )
        writer.execute("INSERT INTO items (itemTypeID, key) VALUES (1, 'WALITEM3')")
        writer.commit()

        dst_dir = tmp_path / "hot_cache"
        dst_dir.mkdir()
        hot = dst_dir / "zotero.sqlite"
        shutil.copy2(src, hot)
        shutil.copy2(str(src) + "-wal", str(hot) + "-wal")
    finally:
        writer.close()

    # Pre-seed a cached snapshot as would occur after a lock-state recovery
    conn_seed = _open_snapshot_connection(hot)
    conn_seed.close()
    assert str(hot) in _sqlite_snapshots
    snapshot_path = _sqlite_snapshots[str(hot)][0]

    # Subsequent call to _open_db must hit the fast path in _sqlite_snapshots
    with _open_db(hot) as conn2:
        assert conn2.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        # Confirm conn2 opened the snapshot DB path, not the original path
        db_filename = conn2.execute("PRAGMA database_list").fetchall()[0]["file"]
        assert db_filename == snapshot_path

    _cleanup_sqlite_snapshots()


# ---------------------------------------------------------------------------
# Task 8: Batch SQLite Queries & Cache Schema Checks Tests
# ---------------------------------------------------------------------------


def _build_item_individually(
    zdb: ZoteroDB, conn: sqlite3.Connection, row: sqlite3.Row | dict
) -> "ZoteroItem":
    """Construct a ZoteroItem using individual queries as baseline reference."""
    from zocrates.rag.zotero_db import ZoteroItem

    item = ZoteroItem(
        item_id=row["itemID"],
        item_key=row["key"],
        item_type=row["typeName"],
        title="",
    )
    zdb._populate_fields(conn, item)
    item.creators = zdb._fetch_creators(conn, row["itemID"])
    item.tags = zdb._fetch_tags(conn, row["itemID"])
    item.collections = zdb._fetch_collections(conn, row["itemID"])
    item.relations = zdb._fetch_relations(conn, row["itemID"])
    return item


def test_batched_vs_individual_item_construction(tmp_path):
    """Verify get_all_items and get_items_by_ids return byte-for-byte identical results to individual queries.

    Verifies across:
      - Fields: title, year/date, abstract, DOI, url, arxiv_id (from extra and from URL), publication precedence
      - Creators: multiple creators, single-name creators ('Plato'), first-name only ('Anonymous')
      - Tags: multiple tags, empty tags
      - Collections: multiple collections, empty collections
      - Relations: destination key extraction, deleted item filtering, duplicate relation deduplication
    """
    db_path = tmp_path / "zotero_batch_parity.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA + "\nCREATE TABLE itemRelations (itemID INTEGER, predicateID INTEGER, object TEXT);")

    # Item types
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (2, 'conferencePaper')")
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (3, 'preprint')")

    # Fields
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'date')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (3, 'abstractNote')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (4, 'DOI')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (5, 'url')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (6, 'publicationTitle')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (7, 'proceedingsTitle')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (8, 'publisher')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (9, 'extra')")

    # Values
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (1, 'Comprehensive AI Survey')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (2, '2024-03-15')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (3, 'Detailed abstract about deep learning architectures.')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (4, 'https://doi.org/10.1038/s41586-024-0001')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (5, 'https://example.com/survey')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (6, 'Nature Machine Intelligence')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (7, 'arXiv: 2403.99999v2')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (8, 'Proceedings of NeurIPS 2023')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (9, 'Curran Associates, Inc.')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (10, 'Ancient Philosophy and Logic')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (11, '380 BCE')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (12, 'https://arxiv.org/abs/2105.12345')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (13, 'Preprint on Attention')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (14, 'Minimal Paper')")

    # Item 10: Rich journal article
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (10, 1, 'KEY10AAA')")
    for fid, vid in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (9, 7)]:
        conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, ?, ?)", (fid, vid))

    # Item 20: Conference paper with publication field precedence (proceedingsTitle > publisher)
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (20, 2, 'KEY20BBB')")
    for fid, vid in [(1, 10), (2, 11), (7, 8), (8, 9)]:
        conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (20, ?, ?)", (fid, vid))

    # Item 30: Deleted item
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (30, 1, 'KEY30DEL')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (30, 1, 14)")
    conn.execute("INSERT INTO deletedItems (itemID) VALUES (30)")

    # Item 40: Minimal item (only title)
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (40, 1, 'KEY40MIN')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (40, 1, 14)")

    # Item 50: Preprint with arxiv URL
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (50, 3, 'KEY50PRE')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (50, 1, 13)")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (50, 5, 12)")

    # Creators
    conn.execute("INSERT INTO creators (creatorID, firstName, lastName) VALUES (101, 'Alex', 'Smith')")
    conn.execute("INSERT INTO creators (creatorID, firstName, lastName) VALUES (102, 'Jane', 'Doe')")
    conn.execute("INSERT INTO creators (creatorID, firstName, lastName) VALUES (103, '', 'Plato')")
    conn.execute("INSERT INTO creators (creatorID, firstName, lastName) VALUES (104, 'Anonymous', '')")

    # Item 10 creators (two authors in order)
    conn.execute("INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (10, 101, 0)")
    conn.execute("INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (10, 102, 1)")
    # Item 20 creator (single-name)
    conn.execute("INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (20, 103, 0)")
    # Item 50 creator (first-name only)
    conn.execute("INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (50, 104, 0)")

    # Tags
    conn.execute("INSERT INTO tags (tagID, name) VALUES (1, 'machine-learning')")
    conn.execute("INSERT INTO tags (tagID, name) VALUES (2, 'deep-learning')")
    conn.execute("INSERT INTO tags (tagID, name) VALUES (3, 'philosophy')")
    conn.execute("INSERT INTO itemTags (itemID, tagID) VALUES (10, 1)")
    conn.execute("INSERT INTO itemTags (itemID, tagID) VALUES (10, 2)")
    conn.execute("INSERT INTO itemTags (itemID, tagID) VALUES (20, 3)")

    # Collections
    conn.execute("INSERT INTO collections (collectionID, collectionName, key) VALUES (1, 'AI', 'COLL_AI')")
    conn.execute("INSERT INTO collections (collectionID, collectionName, key) VALUES (2, 'Vision', 'COLL_VIS')")
    conn.execute("INSERT INTO collectionItems (collectionID, itemID) VALUES (1, 10)")
    conn.execute("INSERT INTO collectionItems (collectionID, itemID) VALUES (2, 10)")

    # Relations
    # Item 10 -> KEY20BBB (valid target)
    conn.execute("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (10, 1, 'http://zotero.org/users/0/items/KEY20BBB')")
    # Item 10 -> KEY30DEL (deleted target, should be excluded)
    conn.execute("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (10, 1, 'http://zotero.org/users/0/items/KEY30DEL')")
    # Item 10 -> KEY20BBB (duplicate relation, should be deduped)
    conn.execute("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (10, 1, 'http://zotero.org/users/0/items/KEY20BBB')")
    # Item 20 -> KEY10AAA
    conn.execute("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (20, 1, 'http://zotero.org/users/0/items/KEY10AAA')")
    # Item 30 (deleted item) -> KEY10AAA (should be excluded)
    conn.execute("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (30, 1, 'http://zotero.org/users/0/items/KEY10AAA')")

    conn.commit()
    conn.close()

    zdb = ZoteroDB(db_path, storage_dir)

    # 1. Build expected items using individual baseline helper
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        sql_rows = """
            SELECT i.itemID, i.key, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE it.typeName != 'attachment'
              AND it.typeName != 'note'
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.itemID
        """
        rows = conn.execute(sql_rows).fetchall()
        expected_items = [_build_item_individually(zdb, conn, r) for r in rows]

    # Verify expected items sanity
    assert len(expected_items) == 4
    assert [it.item_id for it in expected_items] == [10, 20, 40, 50]

    # 2. Fetch using batched get_all_items()
    batched_all = zdb.get_all_items()
    assert batched_all == expected_items

    # 3. Fetch using batched get_items_by_ids()
    active_ids = [10, 20, 40, 50]
    batched_by_ids = zdb.get_items_by_ids(active_ids)
    assert batched_by_ids == expected_items

    # 4. Detailed field-by-field verification across all entities
    for actual, exp in zip(batched_all, expected_items):
        assert actual.item_id == exp.item_id
        assert actual.item_key == exp.item_key
        assert actual.item_type == exp.item_type
        assert actual.title == exp.title
        assert actual.year == exp.year
        assert actual.abstract == exp.abstract
        assert actual.doi == exp.doi
        assert actual.url == exp.url
        assert actual.arxiv_id == exp.arxiv_id
        assert actual.publication == exp.publication
        assert actual.creators == exp.creators
        assert actual.tags == exp.tags
        assert actual.collections == exp.collections
        assert actual.relations == exp.relations

    # Specific property assertions:
    # Item 10: Rich fields
    item10 = next(it for it in batched_all if it.item_id == 10)
    assert item10.title == "Comprehensive AI Survey"
    assert item10.year == "2024"
    assert item10.doi == "10.1038/s41586-024-0001"
    assert item10.arxiv_id == "2403.99999"
    assert item10.publication == "Nature Machine Intelligence"
    assert item10.creators == ["Smith, Alex", "Doe, Jane"]
    assert item10.tags == ["machine-learning", "deep-learning"]
    assert len(item10.collections) == 2
    assert item10.relations == ["KEY20BBB"]  # deleted KEY30DEL excluded, duplicate deduped

    # Item 20: Precedence and single-name creator
    item20 = next(it for it in batched_all if it.item_id == 20)
    assert item20.publication == "Proceedings of NeurIPS 2023"  # proceedingsTitle beats publisher
    assert item20.creators == ["Plato"]
    assert item20.relations == ["KEY10AAA"]

    # Item 40: Minimal
    item40 = next(it for it in batched_all if it.item_id == 40)
    assert item40.creators == []
    assert item40.tags == []
    assert item40.collections == []
    assert item40.relations == []

    # Item 50: URL arXiv ID and first-name only creator
    item50 = next(it for it in batched_all if it.item_id == 50)
    assert item50.arxiv_id == "2105.12345"
    assert item50.creators == ["Anonymous"]


def test_batched_chunking_exceeding_batch_size(tmp_path):
    """Verify batched building handles >500 items across multiple chunks of BATCH_SIZE=500."""
    db_path = tmp_path / "zotero_large_batch.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA + "\nCREATE TABLE itemRelations (itemID INTEGER, predicateID INTEGER, object TEXT);")
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'date')")

    NUM_ITEMS = 550
    item_rows = [(i, 1, f"KEY{i:05d}") for i in range(1, NUM_ITEMS + 1)]
    conn.executemany("INSERT INTO items (itemID, itemTypeID, key) VALUES (?, ?, ?)", item_rows)

    val_rows = []
    data_rows = []
    tag_rows = []
    creator_rows = []
    rel_rows = []

    conn.execute("INSERT INTO tags (tagID, name) VALUES (1, 'common-tag')")

    for i in range(1, NUM_ITEMS + 1):
        # 2 values per item (title, date)
        title_vid = i * 2 - 1
        date_vid = i * 2
        val_rows.append((title_vid, f"Large Batch Paper {i}"))
        val_rows.append((date_vid, f"202{i % 5}-01-01"))
        data_rows.append((i, 1, title_vid))
        data_rows.append((i, 2, date_vid))
        # Creator
        creator_rows.append((i, f"Author{i}", f"Last{i}"))
        # Tag
        tag_rows.append((i, 1))
        # Relation to next item (circular)
        next_i = (i % NUM_ITEMS) + 1
        rel_rows.append((i, 1, f"http://zotero.org/users/0/items/KEY{next_i:05d}"))

    conn.executemany("INSERT INTO itemDataValues (valueID, value) VALUES (?, ?)", val_rows)
    conn.executemany("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?, ?, ?)", data_rows)
    conn.executemany("INSERT INTO creators (creatorID, firstName, lastName) VALUES (?, ?, ?)", creator_rows)
    conn.executemany(
        "INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES (?, ?, 0)",
        [(i, i) for i in range(1, NUM_ITEMS + 1)],
    )
    conn.executemany("INSERT INTO itemTags (itemID, tagID) VALUES (?, ?)", tag_rows)
    conn.executemany("INSERT INTO itemRelations (itemID, predicateID, object) VALUES (?, ?, ?)", rel_rows)

    # Insert deleted items that must be excluded
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (999, 1, 'KEY00999')")
    conn.execute("INSERT INTO deletedItems (itemID) VALUES (999)")

    conn.commit()
    conn.close()

    zdb = ZoteroDB(db_path, storage_dir)

    # Fetch all items across batches (500 in batch 1, 50 in batch 2)
    items = zdb.get_all_items()
    assert len(items) == NUM_ITEMS

    # Check boundaries
    assert items[0].item_id == 1
    assert items[0].title == "Large Batch Paper 1"
    assert items[0].creators == ["Last1, Author1"]
    assert items[0].tags == ["common-tag"]
    assert items[0].relations == ["KEY00002"]

    # Item 500 (end of batch 1)
    assert items[499].item_id == 500
    assert items[499].title == "Large Batch Paper 500"
    assert items[499].creators == ["Last500, Author500"]
    assert items[499].relations == ["KEY00501"]

    # Item 550 (in batch 2)
    assert items[549].item_id == 550
    assert items[549].title == "Large Batch Paper 550"
    assert items[549].creators == ["Last550, Author550"]
    assert items[549].relations == ["KEY00001"]

    # Test get_items_by_ids with 550 items (exceeds single batch chunk of BATCH_SIZE=500)
    all_ids = list(range(1, NUM_ITEMS + 1))
    items_by_ids = zdb.get_items_by_ids(all_ids)
    assert len(items_by_ids) == NUM_ITEMS
    assert items_by_ids == items


def test_query_count_reduction_batched_vs_individual(tmp_path, monkeypatch):
    """Verify batched queries execute O(1) database queries rather than O(N) queries."""
    import zocrates.rag.zotero_db as zdb_module

    db_path = tmp_path / "zotero_query_count.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA + "\nCREATE TABLE itemRelations (itemID INTEGER, predicateID INTEGER, object TEXT);")
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")

    NUM_ITEMS = 50
    for i in range(1, NUM_ITEMS + 1):
        conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (?, 1, ?)", (i, f"K{i:04d}"))
        conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (?, ?)", (i, f"Paper {i}"))
        conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?, 1, ?)", (i, i))

    conn.commit()
    conn.close()

    zdb = ZoteroDB(db_path, storage_dir)

    # 1. Measure queries executed by batched get_all_items()
    batched_queries: list[str] = []
    orig_open = zdb_module._open_db

    @contextmanager
    def spied_open_db(path):
        with orig_open(path) as c:
            c.set_trace_callback(batched_queries.append)
            yield c

    monkeypatch.setattr(zdb_module, "_open_db", spied_open_db)

    items = zdb.get_all_items()
    assert len(items) == NUM_ITEMS

    # Batched execution on first run runs O(1) queries:
    # 1 (items query) + 1 (del_keys) + 5 (fields, creators, tags, collections, relations)
    # plus initial table existence checks
    batched_count = len(batched_queries)
    assert batched_count <= 25, f"Expected O(1) queries (<=25), got {batched_count}"

    # On second run with schema checks cached on self, it runs exactly 7 queries regardless of N
    batched_queries.clear()
    items2 = zdb.get_all_items()
    assert len(items2) == NUM_ITEMS
    assert len(batched_queries) <= 7, f"Expected <= 7 queries with cached schema, got {len(batched_queries)}"

    # 2. Measure queries executed if built individually
    individual_queries: list[str] = []
    with orig_open(db_path) as direct_conn:
        direct_conn.set_trace_callback(individual_queries.append)
        sql_rows = """
            SELECT i.itemID, i.key, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE it.typeName != 'attachment'
              AND it.typeName != 'note'
            ORDER BY i.itemID
        """
        rows = direct_conn.execute(sql_rows).fetchall()
        for r in rows:
            _build_item_individually(zdb, direct_conn, r)

    individual_count = len(individual_queries)
    # 50 items * at least 5 queries per item = 250+ queries
    assert individual_count >= 250, f"Expected individual construction to execute >=250 queries, got {individual_count}"
    assert batched_count < (individual_count / 15), "Batched query count must be more than 15x smaller than individual queries"

    # 3. Verify table existence checks are cached on self
    assert "itemRelations" in zdb._table_exists_cache
    assert "deletedItems" in zdb._table_exists_cache
    assert "items" in zdb._table_exists_cache

    # Subsequent table checks hit memory cache directly without executing SQL
    cache_test_queries: list[str] = []
    with orig_open(db_path) as c2:
        c2.set_trace_callback(cache_test_queries.append)
        assert zdb._table_exists(c2, "itemRelations") is True
        assert zdb._table_exists(c2, "deletedItems") is True
        assert zdb._table_exists(c2, "items") is True
    assert len(cache_test_queries) == 0, "Cached table checks must NOT query sqlite_master"


def test_absent_item_relations_and_deleted_items_schema(tmp_path):
    """Verify behavior when itemRelations or deletedItems tables are absent in SQLite schema."""
    db_path = tmp_path / "zotero_no_relations.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    # Create schema WITHOUT itemRelations table
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ZOTERO_TEST_SCHEMA)  # Note: _ZOTERO_TEST_SCHEMA does not have itemRelations
    conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
    conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (1, 'Paper Without Relations')")
    conn.execute("INSERT INTO items (itemID, itemTypeID, key) VALUES (1, 1, 'KEY00001')")
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (1, 1, 1)")
    conn.commit()
    conn.close()

    zdb = ZoteroDB(db_path, storage_dir)

    # Case A: itemRelations table is absent
    with _open_db(db_path) as c:
        assert zdb._table_exists(c, "itemRelations") is False
        assert zdb._table_exists_cache["itemRelations"] is False

    # All relations queries safely degrade to empty lists without crashing
    assert zdb.get_item_relations(1) == []
    assert zdb.get_item_relations("KEY00001") == []
    assert zdb.get_all_item_relations() == []

    items = zdb.get_all_items()
    assert len(items) == 1
    assert items[0].title == "Paper Without Relations"
    assert items[0].relations == []

    # Case B: deletedItems table is also dropped/absent
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE deletedItems")
    conn.commit()
    conn.close()

    # Reset cache on zdb to observe schema change
    zdb.invalidate_schema_cache()

    with _open_db(db_path) as c:
        assert zdb._table_exists(c, "deletedItems") is False

    # get_all_items, get_items_by_ids, get_item, get_item_by_key, and relation queries succeed
    items_after_drop = zdb.get_all_items()
    assert len(items_after_drop) == 1
    assert items_after_drop[0].item_id == 1

    items_by_id = zdb.get_items_by_ids([1])
    assert len(items_by_id) == 1
    assert items_by_id[0].item_id == 1

    single_item = zdb.get_item(1)
    assert single_item is not None
    assert single_item.item_id == 1

    key_item = zdb.get_item_by_key("KEY00001")
    assert key_item is not None
    assert key_item.item_key == "KEY00001"

    assert zdb.get_item_relations(1) == []
    assert zdb.get_all_item_relations() == []


def test_open_db_closes_connection_on_exit(tmp_path):
    """_open_db context manager must close the SQLite connection upon exit."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    captured_conn = None
    with _open_db(db_path) as c:
        captured_conn = c
        assert c.execute("SELECT 1").fetchone()[0] == 1

    assert captured_conn is not None
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        captured_conn.execute("SELECT 1")


def test_open_db_closes_connection_on_exception(tmp_path):
    """_open_db context manager must close the SQLite connection even if an exception is raised."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    captured_conn = None
    with pytest.raises(RuntimeError, match="simulated failure"):
        with _open_db(db_path) as c:
            captured_conn = c
            raise RuntimeError("simulated failure")

    assert captured_conn is not None
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        captured_conn.execute("SELECT 1")


def test_open_db_closes_probe_connection_on_failure(tmp_path, monkeypatch):
    """When the initial mode=ro probe query fails, its connection must be closed before fallback."""
    from zocrates.rag import zotero_db as zdb_module
    from unittest.mock import MagicMock

    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    mock_probe_conn = MagicMock(spec=sqlite3.Connection)
    mock_probe_conn.execute.side_effect = sqlite3.OperationalError("database is locked")

    orig_connect = zdb_module.sqlite3.connect
    connect_calls = []

    def mock_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        if len(connect_calls) == 1:
            return mock_probe_conn
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(zdb_module.sqlite3, "connect", mock_connect)

    with _open_db(db_path) as c:
        assert c is not None

    mock_probe_conn.close.assert_called_once()


