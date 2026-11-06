"""
Unit tests for LibrarySyncer metadata staleness synchronization and startup registration.

Tests:
  - LibrarySyncer.sync_metadata_staleness() updates stale items and skips fresh items.
  - LibrarySyncer.full_sync() runs sync_metadata_staleness at startup and includes metadata_refreshed in summary.
  - LibrarySyncer.full_sync() handles exceptions during staleness sync gracefully without failing the sync.
  - POST /register launches non-blocking background metadata staleness sweep.
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.rag.document_processor import _open_meta_db
from zocrates.rag.library_syncer import LibrarySyncer
from zocrates.rag.zotero_db import ZoteroDB

_ZOTERO_SCHEMA = """
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY,
    itemTypeID INTEGER,
    key TEXT,
    dateModified TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    clientDateModified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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


def _init_mock_zotero_db(sqlite_path: Path) -> None:
    """Initialize a mock Zotero SQLite database."""
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(_ZOTERO_SCHEMA)
        conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
        conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (2, 'attachment')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'date')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (3, 'abstractNote')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (4, 'DOI')")
        conn.commit()


def _add_zotero_article(
    sqlite_path: Path,
    item_id: int,
    item_key: str,
    title: str,
    creators: list[tuple[str, str]],
    year: str = "2024",
    client_date_modified: str = "2026-08-20 12:00:00",
) -> None:
    """Insert or update a journal article record in the mock Zotero database."""
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO items (itemID, itemTypeID, key, clientDateModified, dateModified) "
            "VALUES (?, 1, ?, ?, ?)",
            (item_id, item_key, client_date_modified, client_date_modified),
        )
        val_id = item_id * 10 + 1
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id, title))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 1, ?)", (item_id, val_id))

        val_id2 = item_id * 10 + 2
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id2, year))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 2, ?)", (item_id, val_id2))

        conn.execute("DELETE FROM itemCreators WHERE itemID = ?", (item_id,))
        for idx, (first, last) in enumerate(creators):
            c_id = item_id * 100 + idx
            conn.execute("INSERT OR REPLACE INTO creators (creatorID, firstName, lastName) VALUES (?, ?, ?)", (c_id, first, last))
            conn.execute("INSERT OR REPLACE INTO itemCreators (itemID, creatorID, orderIndex) VALUES (?, ?, ?)", (item_id, c_id, idx))
        conn.commit()


def _create_mock_bundle(
    cache_dir: Path,
    item_key: str,
    parent_key: str,
    initial_title: str,
    converted_at: str,
) -> Path:
    """Create a mock bundle directory and metadata entry."""
    bundle_dir = cache_dir / item_key
    bundle_dir.mkdir(parents=True, exist_ok=True)
    doc_path = bundle_dir / "document.md"
    doc_content = (
        f"---\n"
        f"type: journalArticle\n"
        f"okf_version: '0.2'\n"
        f"item_key: {item_key}\n"
        f"parent_item_key: {parent_key}\n"
        f"title: {initial_title}\n"
        f"member_of_collections: []\n"
        f"source:\n"
        f"  path: ../../../storage/{item_key}/doc.pdf\n"
        f"  hash: mock_hash_{item_key}\n"
        f"  content_type: application/pdf\n"
        f"provenance:\n"
        f"  converter_id: markitdown\n"
        f"  converter_version: '1.0'\n"
        f"  converted_at: '{converted_at}'\n"
        f"---\n\n## Section\n\nBody content\n"
    )
    doc_path.write_text(doc_content, encoding="utf-8")

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT OR REPLACE INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_key,
                "markitdown",
                "1.0",
                f"storage/{item_key}/doc.pdf",
                "application/pdf",
                "journalArticle",
                converted_at,
                f"mock_hash_{item_key}",
                len(doc_content),
                parent_key,
                initial_title,
            ),
        )
        meta_conn.commit()

    return doc_path


@pytest.fixture
def mock_syncer_environment(tmp_path: Path):
    """Set up an isolated environment with mock Zotero DB, cache, and stubbed LibrarySyncer."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    cache_dir.mkdir(parents=True)

    syncer = LibrarySyncer.__new__(LibrarySyncer)
    syncer._zotero_data_dir = zotero_dir
    syncer.db = ZoteroDB(sqlite_path, storage_dir)

    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir
    mock_processor.list_cached_keys.return_value = []
    mock_processor.ensure_parent_item_key = MagicMock()
    mock_processor.regenerate_root_index = MagicMock()
    syncer.processor = mock_processor

    mock_store = MagicMock()
    mock_store.fts_path = zotero_dir / "zocrates-data" / "fts_index.db"
    mock_store.delete_document = MagicMock()
    syncer.store = mock_store

    return syncer, zotero_dir, cache_dir, sqlite_path


def test_library_syncer_sync_metadata_staleness(mock_syncer_environment):
    """Verify LibrarySyncer.sync_metadata_staleness() updates stale items and skips fresh items."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    now = datetime.now(timezone.utc)
    conv_time = (now - timedelta(days=2)).isoformat()
    mod_stale = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    mod_fresh = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    # Item 1: Stale (modified after conversion)
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="P_STALE",
        title="Stale Article Fresh Title",
        creators=[("Katherine", "Johnson")],
        client_date_modified=mod_stale,
    )
    doc_stale = _create_mock_bundle(cache_dir, "ATT_STALE", "P_STALE", "Old Title Stale", conv_time)

    # Item 2: Fresh (modified before conversion)
    _add_zotero_article(
        sqlite_path,
        item_id=2,
        item_key="P_FRESH",
        title="Fresh Article Title",
        creators=[("Dorothy", "Vaughan")],
        client_date_modified=mod_fresh,
    )
    doc_fresh = _create_mock_bundle(cache_dir, "ATT_FRESH", "P_FRESH", "Fresh Article Title", conv_time)

    # Run staleness sync through syncer
    res = syncer.sync_metadata_staleness()
    assert res["updated"] == 1
    assert res["skipped"] == 0
    assert res["errored"] == 0

    assert "Stale Article Fresh Title" in doc_stale.read_text(encoding="utf-8")
    assert "Fresh Article Title" in doc_fresh.read_text(encoding="utf-8")

    # Second pass: all items up to date, exits immediately
    res_second = syncer.sync_metadata_staleness()
    assert res_second["updated"] == 0
    assert res_second["skipped"] == 2


@pytest.mark.asyncio
async def test_library_syncer_full_sync_includes_metadata_refreshed(mock_syncer_environment):
    """Verify full_sync runs metadata staleness sweep at startup and reports count in summary."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    # Mock sync_metadata_staleness to return 3 updated items
    syncer.sync_metadata_staleness = MagicMock(return_value={"total": 3, "updated": 3, "skipped": 0, "errored": 0})
    syncer._ingest_item = AsyncMock(return_value="skipped")
    syncer._ingest_standalone_note = AsyncMock(return_value="skipped")
    syncer._ingest_standalone_attachment = AsyncMock(return_value="skipped")

    summary = await syncer.full_sync()

    assert syncer.sync_metadata_staleness.called
    assert "metadata_refreshed" in summary
    assert summary["metadata_refreshed"] == 3


@pytest.mark.asyncio
async def test_library_syncer_full_sync_staleness_error_graceful(mock_syncer_environment):
    """Verify full_sync handles staleness sync exceptions gracefully without failing the full sync."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    # Mock sync_metadata_staleness to raise an exception
    syncer.sync_metadata_staleness = MagicMock(side_effect=RuntimeError("Disk I/O error"))
    syncer._ingest_item = AsyncMock(return_value="skipped")
    syncer._ingest_standalone_note = AsyncMock(return_value="skipped")
    syncer._ingest_standalone_attachment = AsyncMock(return_value="skipped")

    summary = await syncer.full_sync()

    assert syncer.sync_metadata_staleness.called
    assert "metadata_refreshed" in summary
    assert summary["metadata_refreshed"] == 0


def test_post_register_launches_background_sweep(tmp_path: Path):
    """Verify POST /register launches non-blocking background metadata staleness sweep."""
    zotero_dir = tmp_path / "zotero_reg"
    zotero_dir.mkdir()
    (zotero_dir / "zotero.sqlite").touch()
    (zotero_dir / "storage").mkdir()

    mock_syncer = MagicMock()
    mock_syncer.sync_metadata_staleness.return_value = {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    with patch("zocrates.fast_api_app.initialize_services", return_value=mock_syncer), \
         patch("zocrates.fast_api_app.services.get_session_service"), \
         patch("zocrates.fast_api_app.services.get_artifact_service"), \
         patch("zocrates.fast_api_app.Runner"):
        with TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
            res = client.post("/register", json={"zotero_data_dir": str(zotero_dir)})
            assert res.status_code == 200
            assert res.json()["status"] == "success"

            # Allow event loop threadpool to execute the background task via bounded poll
            called = False
            for _ in range(50):
                if mock_syncer.sync_metadata_staleness.called:
                    called = True
                    break
                time.sleep(0.02)
            assert called, "Timed out waiting for background staleness sweep to be dispatched"


def test_purge_item_purges_all_four_datastores(mock_syncer_environment):
    """Verify purge_item calls delete_cached, delete_document, and conditionally regenerate_root_index."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    syncer.processor.delete_cached = MagicMock()
    syncer.store.delete_document = MagicMock()
    syncer.processor.regenerate_root_index = MagicMock()

    # Default regenerate_index=True
    syncer.purge_item("PURGE_KEY")

    syncer.processor.delete_cached.assert_called_once_with("PURGE_KEY", update_root_index=False)
    syncer.store.delete_document.assert_called_once_with("PURGE_KEY")
    syncer.processor.regenerate_root_index.assert_called_once()

    # regenerate_index=False skips regenerate_root_index
    syncer.processor.regenerate_root_index.reset_mock()
    syncer.purge_item("PURGE_KEY2", regenerate_index=False)
    syncer.processor.regenerate_root_index.assert_not_called()


def test_sync_metadata_staleness_janitor_purges_orphans(mock_syncer_environment):
    """Verify sync_metadata_staleness purges orphaned items from all datastores."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    # Setup known keys across stores
    syncer.processor.list_cached_keys = MagicMock(return_value=["ACTIVE_KEY"])
    syncer.processor.get_all_meta_keys = MagicMock(return_value={"ORPHAN_KEY"})
    syncer.store.get_all_item_keys = MagicMock(return_value={"ACTIVE_KEY"})

    # Zotero DB reports only ACTIVE_KEY is active
    syncer.db.get_active_item_keys = MagicMock(return_value={"ACTIVE_KEY"})
    syncer.purge_item = MagicMock()
    syncer.processor.regenerate_root_index = MagicMock()

    res = syncer.sync_metadata_staleness()

    syncer.purge_item.assert_called_once_with("ORPHAN_KEY", regenerate_index=False)
    syncer.processor.regenerate_root_index.assert_called_once()
    assert res.get("orphans_cleaned") == 1


def test_sync_metadata_staleness_janitor_skips_when_no_active_keys(mock_syncer_environment):
    """Verify sync_metadata_staleness skips orphan purging if no active items exist in DB."""
    syncer, zotero_dir, cache_dir, sqlite_path = mock_syncer_environment

    syncer.processor.list_cached_keys = MagicMock(return_value=["ORPHAN_KEY"])
    syncer.processor.get_all_meta_keys = MagicMock(return_value=set())
    syncer.store.get_all_item_keys = MagicMock(return_value=set())

    # Zotero DB reports zero active items (e.g. uninitialized or empty DB / wrong profile)
    syncer.db.get_active_item_keys = MagicMock(return_value=set())
    syncer.purge_item = MagicMock()
    syncer.processor.regenerate_root_index = MagicMock()

    res = syncer.sync_metadata_staleness()

    syncer.purge_item.assert_not_called()
    syncer.processor.regenerate_root_index.assert_not_called()
    assert "orphans_cleaned" not in res

