"""
Unit tests for the Frontmatter Refresher and Metadata Synchronization engine.

Tests YAML frontmatter backfilling, atomic disk writes, delta-L line-offset
synchronization across toc.json and SQLite chunk_meta, FTS5 Chunk 0 keyword
updates, Chroma vector-preserving updates, and fast staleness detection.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zocrates.rag.bundle_catalog import _FRONTMATTER_RE
from zocrates.rag.chunker import chunk_markdown
from zocrates.config import get_fts_db_path
from zocrates.rag.document_processor import _open_meta_db
from zocrates.rag.frontmatter_refresher import (
    refresh_bundle_frontmatter,
    sync_stale_metadata,
)
from zocrates.rag.toc_generator import generate_toc, save_toc_files
from zocrates.rag.vector_store import VectorStore, _open_fts_db
from zocrates.rag.zotero_db import ZoteroDB

# Full Zotero test schema including dateModified and clientDateModified
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
    """Initialize a mock Zotero SQLite database with standard tables and fields."""
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(_ZOTERO_SCHEMA)
        conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
        conn.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (2, 'attachment')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (2, 'date')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (3, 'abstractNote')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (4, 'DOI')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (5, 'url')")
        conn.execute("INSERT INTO fields (fieldID, fieldName) VALUES (6, 'publicationTitle')")
        conn.commit()


def _add_zotero_article(
    sqlite_path: Path,
    item_id: int,
    item_key: str,
    title: str,
    creators: list[tuple[str, str]],
    year: str = "2024",
    doi: str | None = None,
    client_date_modified: str = "2026-08-20 12:00:00",
) -> None:
    """Insert or update a journal article record in the mock Zotero database."""
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO items (itemID, itemTypeID, key, clientDateModified, dateModified) "
            "VALUES (?, 1, ?, ?, ?)",
            (item_id, item_key, client_date_modified, client_date_modified),
        )
        # Title
        val_id = item_id * 10 + 1
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id, title))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 1, ?)", (item_id, val_id))

        # Year
        val_id2 = item_id * 10 + 2
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id2, year))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 2, ?)", (item_id, val_id2))

        # DOI
        if doi:
            val_id3 = item_id * 10 + 3
            conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id3, doi))
            conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 4, ?)", (item_id, val_id3))

        # Creators
        conn.execute("DELETE FROM itemCreators WHERE itemID = ?", (item_id,))
        for idx, (first, last) in enumerate(creators):
            c_id = item_id * 100 + idx
            conn.execute("INSERT OR REPLACE INTO creators (creatorID, firstName, lastName) VALUES (?, ?, ?)", (c_id, first, last))
            conn.execute("INSERT OR REPLACE INTO itemCreators (itemID, creatorID, orderIndex) VALUES (?, ?, ?)", (item_id, c_id, idx))
        conn.commit()


# ---------------------------------------------------------------------------
# 1. VectorStore.update_chunk_text_preserve_vector Tests
# ---------------------------------------------------------------------------


def test_vector_store_update_chunk_text_preserve_vector(tmp_path: Path):
    """Verify VectorStore.update_chunk_text_preserve_vector updates text without calling embedder."""
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    initial_vector = [0.42] * 768
    mock_embedder.return_value = [initial_vector]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    # Ingest an initial chunk
    vs._collection.add(
        ids=["ITEMKEY::0"],
        documents=["Original Frontmatter"],
        embeddings=[initial_vector],
        metadatas=[{"item_key": "ITEMKEY", "chunk_index": 0}],
    )

    # Monkeypatch Chroma collection's embedding function to raise if invoked
    def forbidden_embedder(*args, **kwargs):
        raise RuntimeError("CRITICAL ERROR: Embedding function was invoked during text update!")

    vs._collection._embedding_function = forbidden_embedder

    # Update chunk text with vector preservation
    success = vs.update_chunk_text_preserve_vector("ITEMKEY::0", "Updated Frontmatter Text")
    assert success is True

    # Assert document text is updated
    chunk_res = vs._collection.get(ids=["ITEMKEY::0"], include=["documents", "embeddings"])
    assert chunk_res["documents"] == ["Updated Frontmatter Text"]
    # Assert vector embedding is strictly preserved
    assert len(chunk_res["embeddings"]) == 1
    assert chunk_res["embeddings"][0][0] == pytest.approx(0.42, rel=1e-4)

    # Updating nonexistent chunk returns False
    assert vs.update_chunk_text_preserve_vector("NONEXISTENT::0", "Text") is False
    assert vs.update_chunk_text_preserve_vector("", "Text") is False


# ---------------------------------------------------------------------------
# 2. ZoteroDB Timestamp & Staleness Detection Tests
# ---------------------------------------------------------------------------


def test_zotero_db_get_item_date_modified(tmp_path: Path):
    """Verify ZoteroDB.get_item_date_modified returns timezone-aware UTC datetime."""
    sqlite_path = tmp_path / "zotero.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="KEY1",
        title="Paper 1",
        creators=[("Ada", "Lovelace")],
        client_date_modified="2026-08-20 14:30:00",
    )

    db = ZoteroDB(sqlite_path, storage_dir)
    dt1 = db.get_item_date_modified("KEY1")
    assert dt1 is not None
    assert dt1 == datetime(2026, 8, 20, 14, 30, 0, tzinfo=timezone.utc)

    # Nonexistent key returns None
    assert db.get_item_date_modified("UNKNOWN") is None

    # Deleted item returns None
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.execute("INSERT INTO deletedItems (itemID) VALUES (1)")
        conn.commit()
    assert db.get_item_date_modified("KEY1") is None


def test_zotero_db_get_stale_item_keys(tmp_path: Path):
    """Verify ZoteroDB.get_stale_item_keys detects modified items via batched query."""
    sqlite_path = tmp_path / "zotero.sqlite"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    # Article 1: Modified on 2026-08-22
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="PARENT1",
        title="Article 1",
        creators=[("Alan", "Turing")],
        client_date_modified="2026-08-22 10:00:00",
    )
    # Article 2: Modified on 2026-08-15 (older than converted_at)
    _add_zotero_article(
        sqlite_path,
        item_id=2,
        item_key="PARENT2",
        title="Article 2",
        creators=[("Claude", "Shannon")],
        client_date_modified="2026-08-15 10:00:00",
    )

    db = ZoteroDB(sqlite_path, storage_dir)

    key_to_atts = {
        "PARENT1": ["ATT1_1", "ATT1_2"],
        "PARENT2": ["ATT2"],
    }
    converted_map = {
        "ATT1_1": "2026-08-20T12:00:00+00:00",  # Stale (parent mod: 2026-08-22)
        "ATT1_2": "2026-08-20T12:00:00+00:00",  # Stale
        "ATT2": "2026-08-20T12:00:00+00:00",    # Not stale (parent mod: 2026-08-15)
    }

    stale_keys = db.get_stale_item_keys(converted_map, key_to_atts)
    assert stale_keys == ["ATT1_1", "ATT1_2"]


# ---------------------------------------------------------------------------
# 3. refresh_bundle_frontmatter Unit Tests
# ---------------------------------------------------------------------------


def test_refresh_bundle_frontmatter_single_item(tmp_path: Path):
    """Verify refresh_bundle_frontmatter updates document.md, toc.json, FTS5, and metadata."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    # Mock initial Zotero metadata
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="PARENT1",
        title="Initial Title",
        creators=[("Alice", "Smith")],
        year="2021",
        doi="10.1234/initial",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "ATT1"
    bundle_dir.mkdir(parents=True)

    # Create conversion_meta.db record
    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ATT1",
                "markitdown",
                "1.0",
                "storage/ATT1/doc.pdf",
                "application/pdf",
                "journalArticle",
                "2026-08-18T10:00:00+00:00",
                "abc123hash",
                100,
                "PARENT1",
                "Initial Title",
            ),
        )
        meta_conn.commit()

    initial_doc = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: ATT1\n"
        "parent_item_key: PARENT1\n"
        "title: Initial Title\n"
        "authors:\n"
        "- Alice Smith\n"
        "year: 2021\n"
        "doi: 10.1234/initial\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/ATT1/doc.pdf\n"
        "  hash: abc123hash\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n"
        "\n"
        "## Section 1: Introduction\n"
        "\n"
        "This is the body content of the paper that must be preserved.\n"
    )
    doc_path = bundle_dir / "document.md"
    doc_path.write_text(initial_doc, encoding="utf-8")

    # Initialize FTS5 database with Chunk 0 and body Chunk 1
    fts_path = get_fts_db_path(zotero_dir)
    fts_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_fts_db(fts_path) as conn:
        conn.execute(
            "INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, line_start, line_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ATT1::0", "ATT1", 1, 0, "frontmatter", 1, 20),
        )
        conn.execute(
            "INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, line_start, line_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ATT1::1", "ATT1", 1, 1, "text", 22, 26),
        )
        conn.execute(
            "INSERT INTO chunks_fts (chunk_id, item_key, chunk_text) VALUES (?, ?, ?)",
            ("ATT1::0", "ATT1", "Initial Title Alice Smith"),
        )
        conn.commit()

    # Now simulate Zotero edit: Update title, add 2 authors (expanding frontmatter lines)
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="PARENT1",
        title="Revised Groundbreaking Title",
        creators=[("Alice", "Smith"), ("Bob", "Jones"), ("Carol", "White")],
        year="2022",
        doi="10.1234/revised.doi",
        client_date_modified="2026-08-22 15:00:00",
    )

    # Execute refresh
    result = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=["ATT1"])
    assert result == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    # 1. Assert document.md has new frontmatter and byte-identical body
    refreshed_content = doc_path.read_text(encoding="utf-8")
    assert "Revised Groundbreaking Title" in refreshed_content
    assert "Jones, Bob" in refreshed_content
    assert "White, Carol" in refreshed_content
    assert "10.1234/revised.doi" in refreshed_content
    assert "## Section 1: Introduction\n\nThis is the body content of the paper that must be preserved.\n" in refreshed_content

    # 2. Assert toc.json exists and is valid
    toc_json_path = bundle_dir / "toc.json"
    assert toc_json_path.exists()
    toc_data = json.loads(toc_json_path.read_text(encoding="utf-8"))
    assert toc_data["title"] == "Revised Groundbreaking Title"

    # 3. Assert chunk_meta was shifted by delta_l
    with _open_fts_db(fts_path) as conn:
        c0 = conn.execute("SELECT * FROM chunk_meta WHERE chunk_id = 'ATT1::0'").fetchone()
        c1 = conn.execute("SELECT * FROM chunk_meta WHERE chunk_id = 'ATT1::1'").fetchone()
        # Frontmatter expanded by 2 author lines -> delta_l = 2
        new_fm_lines = len(_FRONTMATTER_RE.match(refreshed_content).group(0).rstrip("\r\n").splitlines())
        assert c0["line_end"] == new_fm_lines
        assert c1["line_start"] == 22 + 2
        assert c1["line_end"] == 26 + 2

        # 4. Assert chunks_fts Chunk 0 has fresh text
        fts_row = conn.execute("SELECT chunk_text FROM chunks_fts WHERE chunk_id = 'ATT1::0'").fetchone()
        assert "Revised Groundbreaking Title" in fts_row["chunk_text"]
        assert "White, Carol" in fts_row["chunk_text"]

    # 5. Assert conversion_meta.db updated
    with _open_meta_db(cache_dir) as meta_conn:
        meta_row = meta_conn.execute("SELECT title FROM conversion_meta WHERE item_key = 'ATT1'").fetchone()
        assert meta_row["title"] == "Revised Groundbreaking Title"

    # 6. Assert immediate second refresh is zero-write NO-OP
    mtime_before = doc_path.stat().st_mtime
    result_noop = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=["ATT1"])
    assert result_noop == {"total": 1, "updated": 0, "skipped": 1, "errored": 0}
    assert doc_path.stat().st_mtime == mtime_before


def test_refresh_bundle_frontmatter_dry_run(tmp_path: Path):
    """Verify dry_run=True previews changes without writing to disk or database."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="KEY_DRY",
        title="Dry Run Changed Title",
        creators=[("David", "Bohm")],
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "KEY_DRY"
    bundle_dir.mkdir(parents=True)

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KEY_DRY", "markitdown", "1.0", "storage/KEY_DRY/doc.pdf", "application/pdf", "journalArticle",
             "2026-08-18T10:00:00+00:00", "hash1", 50, "KEY_DRY", "Original Title"),
        )
        meta_conn.commit()

    doc_path = bundle_dir / "document.md"
    original_text = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: KEY_DRY\n"
        "parent_item_key: KEY_DRY\n"
        "title: Original Title\n"
        "authors:\n"
        "- David Bohm\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/KEY_DRY/doc.pdf\n"
        "  hash: hash1\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n\nBody text.\n"
    )
    doc_path.write_text(original_text, encoding="utf-8")

    result = refresh_bundle_frontmatter(zotero_dir=zotero_dir, dry_run=True)
    assert result == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    # Document must remain untouched
    assert doc_path.read_text(encoding="utf-8") == original_text

    # conversion_meta.db must remain untouched
    with _open_meta_db(cache_dir) as meta_conn:
        row = meta_conn.execute("SELECT title FROM conversion_meta WHERE item_key = 'KEY_DRY'").fetchone()
        assert row["title"] == "Original Title"


def test_sync_stale_metadata_sweep(tmp_path: Path):
    """Verify sync_stale_metadata sweeps across items and only updates stale ones."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    now = datetime.now(timezone.utc)
    conv_time = (now - timedelta(days=2)).isoformat()
    mod_stale = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    mod_fresh = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    # Item 1: Modified recently (STALE: mod > conv)
    _add_zotero_article(
        sqlite_path,
        item_id=1,
        item_key="PARENT_STALE",
        title="Stale Article Modified",
        creators=[("Grace", "Hopper")],
        client_date_modified=mod_stale,
    )
    # Item 2: Not modified since conversion (FRESH: mod < conv)
    _add_zotero_article(
        sqlite_path,
        item_id=2,
        item_key="PARENT_FRESH",
        title="Fresh Article Untouched",
        creators=[("Ada", "Lovelace")],
        client_date_modified=mod_fresh,
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"

    for key, p_key, title in [("ATT_STALE", "PARENT_STALE", "Old Title Stale"), ("ATT_FRESH", "PARENT_FRESH", "Fresh Article Untouched")]:
        b_dir = cache_dir / key
        b_dir.mkdir(parents=True)
        doc = (
            f"---\n"
            f"type: journalArticle\n"
            f"okf_version: '0.2'\n"
            f"item_key: {key}\n"
            f"parent_item_key: {p_key}\n"
            f"title: {title}\n"
            f"member_of_collections: []\n"
            f"source:\n"
            f"  path: ../../../storage/{key}/doc.pdf\n"
            f"  hash: hash\n"
            f"  content_type: application/pdf\n"
            f"provenance:\n"
            f"  converter_id: markitdown\n"
            f"  converter_version: '1.0'\n"
            f"  converted_at: '{conv_time}'\n"
            f"---\n\nBody content\n"
        )
        (b_dir / "document.md").write_text(doc, encoding="utf-8")

        with _open_meta_db(cache_dir) as meta_conn:
            meta_conn.execute(
                "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
                "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, "markitdown", "1.0", f"storage/{key}/doc.pdf", "application/pdf", "journalArticle",
                 conv_time, "hash", 50, p_key, title),
            )
            meta_conn.commit()

    # Initial staleness sweep
    res = sync_stale_metadata(zotero_dir=zotero_dir)
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    # Verify ATT_STALE document was refreshed
    refreshed_stale = (cache_dir / "ATT_STALE" / "document.md").read_text(encoding="utf-8")
    assert "Stale Article Modified" in refreshed_stale
    assert "Hopper, Grace" in refreshed_stale

    # Verify ATT_FRESH document was NOT touched
    fresh_doc = (cache_dir / "ATT_FRESH" / "document.md").read_text(encoding="utf-8")
    assert "Fresh Article Untouched" in fresh_doc

    # Immediate second sweep: all items are now up to date, exits immediately
    res_second = sync_stale_metadata(zotero_dir=zotero_dir)
    assert res_second == {"total": 2, "updated": 0, "skipped": 2, "errored": 0}


# ---------------------------------------------------------------------------
# 4. Real-World Document Fixture Tests (Permanent Fixtures Treated as Read-Only)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file on disk."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_refresh_real_world_docling_bundle_with_line_shift(tmp_path: Path):
    """Verify ARSFJKPA Docling bundle frontmatter refresh, line shifts, and read-only fixture invariance."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "documents" / "ARSFJKPA_document.md"
    assert fixture_path.exists(), f"Fixture missing: {fixture_path}"

    initial_fixture_hash = _sha256_file(fixture_path)

    # Setup isolated test directory
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    item_key = "ARSFJKPA"
    parent_key = "9N8KQEZI"

    # Initial Zotero article record
    _add_zotero_article(
        sqlite_path,
        item_id=100,
        item_key=parent_key,
        title="Differences between germline genomes of monozygotic twins",
        creators=[("Hakon", "Jonsson"), ("Erna", "Magnusdottir")],
        year="2021",
        doi="10.1038/s41588-020-00755-1",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / item_key
    bundle_dir.mkdir(parents=True)

    # COPY fixture content into isolated bundle directory (NEVER modify fixture in-place!)
    raw_fixture_text = fixture_path.read_text(encoding="utf-8")
    doc_path = bundle_dir / "document.md"
    doc_path.write_text(raw_fixture_text, encoding="utf-8")

    # Ingest chunks into chunk_meta and chunks_fts
    chunks = chunk_markdown(raw_fixture_text, document_title="Differences between germline genomes of monozygotic twins")
    assert len(chunks) == 59  # 1 frontmatter + 58 body chunks

    fts_path = get_fts_db_path(zotero_dir)
    fts_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_fts_db(fts_path) as conn:
        for c in chunks:
            conn.execute(
                "INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, line_start, line_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{item_key}::{c.chunk_index}", item_key, 100, c.chunk_index, c.chunk_type, c.line_start, c.line_end),
            )
            conn.execute(
                "INSERT INTO chunks_fts (chunk_id, item_key, chunk_text) VALUES (?, ?, ?)",
                (f"{item_key}::{c.chunk_index}", item_key, c.text),
            )
        conn.commit()

    # Create conversion_meta.db record
    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_key,
                "docling_local",
                "2.120.1-fast",
                "storage/ARSFJKPA/doc.pdf",
                "application/pdf",
                "journalArticle",
                "2026-08-18T14:42:07.263641+00:00",
                "45c127e5da7132b0bf8c03674e62743d14fcf2f65aa4d7e02f4f868d260417ab",
                len(raw_fixture_text),
                parent_key,
                "Differences between germline genomes of monozygotic twins",
            ),
        )
        meta_conn.commit()

    # Record initial line numbers of Chunk 1 and Chunk 58
    with _open_fts_db(fts_path) as conn:
        c1_before = conn.execute(f"SELECT line_start, line_end FROM chunk_meta WHERE chunk_id = '{item_key}::1'").fetchone()
        c58_before = conn.execute(f"SELECT line_start, line_end FROM chunk_meta WHERE chunk_id = '{item_key}::58'").fetchone()

    # Simulate Zotero metadata change: Add 4 new authors and a new DOI
    _add_zotero_article(
        sqlite_path,
        item_id=100,
        item_key=parent_key,
        title="Differences between germline genomes of monozygotic twins (Updated)",
        creators=[
            ("Hakon", "Jonsson"),
            ("Erna", "Magnusdottir"),
            ("Hannes", "Eggertsson"),
            ("Olafur", "Stefansson"),
            ("Gudny", "Arnadottir"),
            ("Ogmundur", "Eiriksson"),
        ],
        year="2021",
        doi="10.1038/s41588-020-00755-1-updated",
        client_date_modified="2026-08-25 12:00:00",
    )

    # Execute refresh
    res = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=[item_key])
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    refreshed_text = doc_path.read_text(encoding="utf-8")
    assert "Differences between germline genomes of monozygotic twins (Updated)" in refreshed_text
    assert "Ogmundur Eiriksson" in refreshed_text

    # Calculate actual delta_l
    old_fm_match = _FRONTMATTER_RE.match(raw_fixture_text)
    new_fm_match = _FRONTMATTER_RE.match(refreshed_text)
    old_fm_lines = len(old_fm_match.group(0).rstrip("\r\n").splitlines())
    new_fm_lines = len(new_fm_match.group(0).rstrip("\r\n").splitlines())
    delta_l = new_fm_lines - old_fm_lines
    assert delta_l > 0

    # Verify that all 58 body chunks were shifted by exactly delta_l
    with _open_fts_db(fts_path) as conn:
        c1_after = conn.execute(f"SELECT line_start, line_end FROM chunk_meta WHERE chunk_id = '{item_key}::1'").fetchone()
        c58_after = conn.execute(f"SELECT line_start, line_end FROM chunk_meta WHERE chunk_id = '{item_key}::58'").fetchone()

        assert c1_after["line_start"] == c1_before["line_start"] + delta_l
        assert c1_after["line_end"] == c1_before["line_end"] + delta_l
        assert c58_after["line_start"] == c58_before["line_start"] + delta_l
        assert c58_after["line_end"] == c58_before["line_end"] + delta_l

    # Second refresh must be zero-op
    res_noop = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=[item_key])
    assert res_noop == {"total": 1, "updated": 0, "skipped": 1, "errored": 0}

    # CRUCIAL: Verify fixture on disk was NEVER modified!
    final_fixture_hash = _sha256_file(fixture_path)
    assert initial_fixture_hash == final_fixture_hash, "Regression: Fixture file was modified in-place!"


def test_refresh_real_world_datalab_bundle_preserves_captions(tmp_path: Path):
    """Verify QNVRFYQM Datalab bundle preserves synthetic captions and read-only fixture invariance."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "documents" / "QNVRFYQM_document.md"
    assert fixture_path.exists(), f"Fixture missing: {fixture_path}"

    initial_fixture_hash = _sha256_file(fixture_path)

    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    item_key = "QNVRFYQM"
    parent_key = "X88DZ9HX"

    _add_zotero_article(
        sqlite_path,
        item_id=200,
        item_key=parent_key,
        title="Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure",
        creators=[("Zhicong", "Wang"), ("Zicheng", "Ma")],
        year="2025",
        doi="10.1093/bioinformatics/btad123",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / item_key
    bundle_dir.mkdir(parents=True)

    raw_fixture_text = fixture_path.read_text(encoding="utf-8")
    doc_path = bundle_dir / "document.md"
    doc_path.write_text(raw_fixture_text, encoding="utf-8")

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_key,
                "datalab",
                "0.5.0",
                "storage/QNVRFYQM/doc.pdf",
                "application/pdf",
                "preprint",
                "2026-08-19T21:12:29.358554+00:00",
                "4cb4d6d3db604e71e1dcdd6e6d8daeb6979290369cd07d4ecd416be07d44164f",
                len(raw_fixture_text),
                parent_key,
                "Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure",
            ),
        )
        meta_conn.commit()

    # Simulate updated Zotero metadata with revised DOI and publisher
    _add_zotero_article(
        sqlite_path,
        item_id=200,
        item_key=parent_key,
        title="Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure (Revised)",
        creators=[("Zhicong", "Wang"), ("Zicheng", "Ma"), ("Ziqiang", "Cao")],
        year="2025",
        doi="10.1093/bioinformatics/btad999",
        client_date_modified="2026-08-25 18:00:00",
    )

    res = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=[item_key])
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    refreshed_text = doc_path.read_text(encoding="utf-8")
    assert "Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure (Revised)" in refreshed_text
    assert "10.1093/bioinformatics/btad999" in refreshed_text

    # Extract body content from original and refreshed text
    orig_body = raw_fixture_text[_FRONTMATTER_RE.match(raw_fixture_text).end() :]
    new_body = refreshed_text[_FRONTMATTER_RE.match(refreshed_text).end() :]

    # Assert body text (including VLM synthetic captions, figures, equations) is 100% byte-identical
    assert new_body == orig_body
    assert "<synthetic_caption>" in new_body
    assert "Diagram illustrating the Prot2Chat framework." in new_body

    # CRUCIAL: Verify fixture on disk was NEVER modified!
    final_fixture_hash = _sha256_file(fixture_path)
    assert initial_fixture_hash == final_fixture_hash, "Regression: Fixture file was modified in-place!"


# ---------------------------------------------------------------------------
# 5. Chroma Vector-Preservation End-to-End Tests
# ---------------------------------------------------------------------------


def test_refresh_bundle_frontmatter_with_vector_store(tmp_path: Path):
    """Verify refresh_bundle_frontmatter propagates title and text to Chroma without re-embedding."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=50,
        item_key="KEY_VS",
        title="Initial Vector Paper",
        creators=[("Enrico", "Fermi")],
        year="2020",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "KEY_VS"
    bundle_dir.mkdir(parents=True)

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KEY_VS", "markitdown", "1.0", "storage/KEY_VS/doc.pdf", "application/pdf", "journalArticle",
             "2026-08-18T10:00:00+00:00", "hash_vs", 100, "KEY_VS", "Initial Vector Paper"),
        )
        meta_conn.commit()

    doc_text = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: KEY_VS\n"
        "parent_item_key: KEY_VS\n"
        "title: Initial Vector Paper\n"
        "authors:\n"
        "- Fermi, Enrico\n"
        "year: 2020\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/KEY_VS/doc.pdf\n"
        "  hash: hash_vs\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n\n## Section 1\nBody text.\n"
    )
    (bundle_dir / "document.md").write_text(doc_text, encoding="utf-8")

    # Setup VectorStore
    chroma_dir = tmp_path / "chroma"
    fts_path = get_fts_db_path(zotero_dir)
    fts_path.parent.mkdir(parents=True, exist_ok=True)

    mock_embedder = MagicMock()
    mock_embedder._model_name = "jinaai/jina-embeddings-v5-text-nano"
    mock_embedder.dimension = 768
    vector_768 = [0.123] * 768
    mock_embedder.return_value = [vector_768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_path, embedder=mock_embedder)

    # Ingest Chunk 0 and Chunk 1 into VectorStore
    vs._collection.add(
        ids=["KEY_VS::0", "KEY_VS::1"],
        documents=["Initial Frontmatter Text", "Body text."],
        embeddings=[vector_768, [0.456] * 768],
        metadatas=[
            {"item_key": "KEY_VS", "chunk_index": 0, "document_title": "Initial Vector Paper"},
            {"item_key": "KEY_VS", "chunk_index": 1, "document_title": "Initial Vector Paper"},
        ],
    )

    # Monkeypatch collection embedding function to guarantee zero ML invocation
    def forbidden_embedder(*args, **kwargs):
        raise RuntimeError("CRITICAL ERROR: Embedding function was invoked during frontmatter refresh!")

    vs._collection._embedding_function = forbidden_embedder

    # Update Zotero metadata with new title and author
    _add_zotero_article(
        sqlite_path,
        item_id=50,
        item_key="KEY_VS",
        title="Updated Quantum Theory Title",
        creators=[("Enrico", "Fermi"), ("Paul", "Dirac")],
        year="2021",
    )

    # Execute refresh with store=vs
    res = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=["KEY_VS"], store=vs)
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    # Verify Chroma Chunk 0 text was updated
    c0 = vs._collection.get(ids=["KEY_VS::0"], include=["documents", "embeddings", "metadatas"])
    assert "Updated Quantum Theory Title" in c0["documents"][0]
    assert "Dirac, Paul" in c0["documents"][0]
    # Verify embedding is intact
    assert c0["embeddings"][0][0] == pytest.approx(0.123, rel=1e-4)

    # Verify document_title was updated across all chunks in Chroma metadata
    c1 = vs._collection.get(ids=["KEY_VS::1"], include=["metadatas"])
    assert c1["metadatas"][0]["document_title"] == "Updated Quantum Theory Title"


def test_refresh_bundle_frontmatter_store_none(tmp_path: Path):
    """Verify refresh_bundle_frontmatter succeeds cleanly when store=None (CLI path)."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=60,
        item_key="KEY_NO_STORE",
        title="Paper Without Store",
        creators=[("Niels", "Bohr")],
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "KEY_NO_STORE"
    bundle_dir.mkdir(parents=True)

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KEY_NO_STORE", "markitdown", "1.0", "storage/KEY_NO_STORE/doc.pdf", "application/pdf", "journalArticle",
             "2026-08-18T10:00:00+00:00", "hash_ns", 100, "KEY_NO_STORE", "Paper Without Store"),
        )
        meta_conn.commit()

    doc_text = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: KEY_NO_STORE\n"
        "parent_item_key: KEY_NO_STORE\n"
        "title: Paper Without Store\n"
        "authors:\n"
        "- Bohr, Niels\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/KEY_NO_STORE/doc.pdf\n"
        "  hash: hash_ns\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n\nBody content\n"
    )
    (bundle_dir / "document.md").write_text(doc_text, encoding="utf-8")

    # Update metadata
    _add_zotero_article(
        sqlite_path,
        item_id=60,
        item_key="KEY_NO_STORE",
        title="Updated Paper Without Store",
        creators=[("Niels", "Bohr"), ("Werner", "Heisenberg")],
    )

    # store=None must not fail or attempt to instantiate an embedder
    res = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=["KEY_NO_STORE"], store=None)
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    refreshed = (bundle_dir / "document.md").read_text(encoding="utf-8")
    assert "Updated Paper Without Store" in refreshed
    assert "Heisenberg, Werner" in refreshed


def test_refresh_bundle_frontmatter_sanitize_images(tmp_path: Path):
    """Verify refresh_bundle_frontmatter with sanitize_images=True rewrites file:/// links while keeping line count invariant."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=70,
        item_key="KEY_IMG",
        title="Sanitized Image Paper",
        creators=[("Ada", "Lovelace")],
        year="2021",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "KEY_IMG"
    bundle_dir.mkdir(parents=True)

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KEY_IMG", "docling_local", "2.12.0", "storage/KEY_IMG/doc.pdf", "application/pdf", "journalArticle",
             "2026-08-18T10:00:00+00:00", "hash_img", 200, "KEY_IMG", "Sanitized Image Paper"),
        )
        meta_conn.commit()

    initial_body = (
        "## Introduction\n\n"
        "Here is a diagram:\n\n"
        "![Figure 1](file:///home/alex/Zotero/storage/KEY_IMG/images/picture_001.png)\n\n"
        "And another plot with spaces:\n\n"
        "![Figure 2](file:///home/alex/Zotero/storage/KEY_IMG/images/plot%20alpha.png)\n\n"
        "End of section.\n"
    )

    doc_text = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: KEY_IMG\n"
        "parent_item_key: KEY_IMG\n"
        "title: Sanitized Image Paper\n"
        "authors:\n"
        "- Lovelace, Ada\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/KEY_IMG/doc.pdf\n"
        "  hash: hash_img\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: docling_local\n"
        "  converter_version: 2.12.0\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n"
        + initial_body
    )
    (bundle_dir / "document.md").write_text(doc_text, encoding="utf-8")
    initial_body_line_count = len(initial_body.splitlines())

    # Refresh with sanitize_images=True
    res = refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        target_keys=["KEY_IMG"],
        sanitize_images=True,
    )
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    refreshed = (bundle_dir / "document.md").read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(refreshed)
    assert fm_match is not None
    refreshed_body = refreshed[fm_match.end() :]

    # Assert links were rewritten cleanly
    assert "images/picture_001.png" in refreshed_body
    assert "images/plot alpha.png" in refreshed_body
    assert "file://" not in refreshed_body

    # Assert strict line-count invariance of body text
    refreshed_body_line_count = len(refreshed_body.splitlines())
    assert refreshed_body_line_count == initial_body_line_count


def test_chunk_meta_drift_self_healing(tmp_path: Path):
    """Verify that drifted line_end in chunk_meta triggers self-healing even if frontmatter content matches."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    _add_zotero_article(
        sqlite_path,
        item_id=80,
        item_key="KEY_DRIFT",
        title="Drift Test Paper",
        creators=[("Alan", "Turing")],
        year="1950",
    )

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    bundle_dir = cache_dir / "KEY_DRIFT"
    bundle_dir.mkdir(parents=True)

    with _open_meta_db(cache_dir) as meta_conn:
        meta_conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
            "content_type, parent_item_type, converted_at, source_hash, char_count, parent_item_key, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KEY_DRIFT", "markitdown", "1.0", "storage/KEY_DRIFT/doc.pdf", "application/pdf", "journalArticle",
             "2026-08-18T10:00:00+00:00", "hash_drift", 100, "KEY_DRIFT", "Drift Test Paper"),
        )
        meta_conn.commit()

    doc_text = (
        "---\n"
        "type: journalArticle\n"
        "okf_version: '0.2'\n"
        "item_key: KEY_DRIFT\n"
        "parent_item_key: KEY_DRIFT\n"
        "title: Drift Test Paper\n"
        "authors:\n"
        "- Turing, Alan\n"
        "year: '1950'\n"
        "member_of_collections: []\n"
        "source:\n"
        "  path: ../../../storage/KEY_DRIFT/doc.pdf\n"
        "  hash: hash_drift\n"
        "  content_type: application/pdf\n"
        "provenance:\n"
        "  converter_id: markitdown\n"
        "  converter_version: '1.0'\n"
        "  converted_at: '2026-08-18T10:00:00+00:00'\n"
        "---\n\n## Section 1\n\nContent here.\n"
    )
    (bundle_dir / "document.md").write_text(doc_text, encoding="utf-8")

    # Generate valid ToC
    toc_json, toc_md = generate_toc(
        document_md=doc_text,
        title="Drift Test Paper",
        abstract=None,
        abstract_source="extractive",
        item_key="KEY_DRIFT",
        parent_item_key="KEY_DRIFT",
    )
    save_toc_files(bundle_dir, toc_json, toc_md)

    # Ingest into SQLite FTS5 with artificially corrupted line_end = 999 on Chunk 0
    fts_path = get_fts_db_path(zotero_dir)
    with _open_fts_db(fts_path) as conn:
        conn.execute(
            "INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, line_start, line_end) "
            "VALUES (?, ?, 80, 0, 'frontmatter', 1, 999)",
            ("KEY_DRIFT::0", "KEY_DRIFT"),
        )
        conn.execute(
            "INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, line_start, line_end) "
            "VALUES (?, ?, 80, 1, 'heading', 1000, 1001)",
            ("KEY_DRIFT::1", "KEY_DRIFT"),
        )
        conn.commit()

    # Frontmatter content matches perfectly, but chunk_meta has drifted line_end (999 vs 18)
    res = refresh_bundle_frontmatter(zotero_dir=zotero_dir, target_keys=["KEY_DRIFT"])
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    # Verify Chunk 0 line_end and body chunk boundaries were healed in chunk_meta
    with _open_fts_db(fts_path) as conn:
        row0 = conn.execute(
            "SELECT line_end FROM chunk_meta WHERE item_key = 'KEY_DRIFT' AND chunk_index = 0"
        ).fetchone()
        fm_lines = len(_FRONTMATTER_RE.match(doc_text).group(0).rstrip("\r\n").splitlines())
        assert row0["line_end"] == fm_lines

        row1 = conn.execute(
            "SELECT line_start, line_end FROM chunk_meta WHERE item_key = 'KEY_DRIFT' AND chunk_index = 1"
        ).fetchone()
        # chunk 1 started at 1000, shifted by (18 - 999) = -981 -> line_start = 19, line_end = 20
        assert row1["line_start"] == 1000 + (fm_lines - 999)
        assert row1["line_end"] == 1001 + (fm_lines - 999)

