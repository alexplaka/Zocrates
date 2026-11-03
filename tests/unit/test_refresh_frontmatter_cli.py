"""
Unit tests for the CLI wrapper in scripts/refresh_frontmatter.py.

Tests:
  - Backward-compatible bridge function `refresh_frontmatters(...)`.
  - CLI invocation with `--dry-run`.
  - CLI invocation with `--stale-only`.
  - CLI invocation with specific item keys.
  - CLI invocation with `--sanitize-images`.
  - Data directory lookup via `find_zotero_dir`.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zocrates.rag.document_processor import _open_meta_db
from scripts.refresh_frontmatter import find_zotero_dir, main, refresh_frontmatters

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
        val_id = item_id * 10 + 1
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id, title))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 1, ?)", (item_id, val_id))

        val_id2 = item_id * 10 + 2
        conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id2, year))
        conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 2, ?)", (item_id, val_id2))

        if doi:
            val_id3 = item_id * 10 + 3
            conn.execute("INSERT OR REPLACE INTO itemDataValues (valueID, value) VALUES (?, ?)", (val_id3, doi))
            conn.execute("INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, 4, ?)", (item_id, val_id3))

        conn.execute("DELETE FROM itemCreators WHERE itemID = ?", (item_id,))
        for idx, (first, last) in enumerate(creators):
            c_id = item_id * 100 + idx
            conn.execute("INSERT OR REPLACE INTO creators (creatorID, firstName, lastName) VALUES (?, ?, ?)", (c_id, first, last))
            conn.execute("INSERT OR REPLACE INTO itemCreators (itemID, creatorID, orderIndex) VALUES (?, ?, ?)", (item_id, c_id, idx))
        conn.commit()


@pytest.fixture
def mock_zotero_environment(tmp_path: Path):
    """Set up a temporary Zotero environment with mock database and cache."""
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    storage_dir.mkdir()
    _init_mock_zotero_db(sqlite_path)

    cache_dir = zotero_dir / "zocrates-data" / "corpus"
    cache_dir.mkdir(parents=True)

    return zotero_dir, cache_dir, sqlite_path


def _create_mock_bundle(
    cache_dir: Path,
    item_key: str,
    parent_key: str,
    initial_title: str,
    converted_at: str = "2026-08-18T10:00:00+00:00",
    extra_body: str = "Sample body text.\n",
) -> Path:
    """Helper to create a mock markdown bundle on disk and register it in conversion_meta.db."""
    b_dir = cache_dir / item_key
    b_dir.mkdir(parents=True, exist_ok=True)
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
        f"---\n\n{extra_body}"
    )
    doc_path = b_dir / "document.md"
    doc_path.write_text(doc_content, encoding="utf-8")

    with _open_meta_db(cache_dir) as conn:
        conn.execute(
            "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path, "
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
        conn.commit()

    return doc_path


def test_refresh_frontmatters_bridge(mock_zotero_environment):
    """Verify refresh_frontmatters bridge function forwards correctly to refresh_bundle_frontmatter."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    _add_zotero_article(
        sqlite_path,
        item_id=10,
        item_key="PARENT_1",
        title="Updated Article 1",
        creators=[("Marie", "Curie")],
        year="1903",
        doi="10.1000/182",
    )
    doc_path = _create_mock_bundle(cache_dir, "ATT_1", "PARENT_1", "Old Article 1")

    res = refresh_frontmatters(zotero_dir, target_keys=["ATT_1"], dry_run=False, sanitize_images=False)
    assert res == {"total": 1, "updated": 1, "skipped": 0, "errored": 0}

    refreshed_content = doc_path.read_text(encoding="utf-8")
    assert "Updated Article 1" in refreshed_content
    assert "Curie, Marie" in refreshed_content
    assert "10.1000/182" in refreshed_content


def test_cli_dry_run(mock_zotero_environment, capsys):
    """Verify CLI --dry-run prints preview without modifying files on disk."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    _add_zotero_article(
        sqlite_path,
        item_id=20,
        item_key="PARENT_DRY",
        title="Dry Run Article Title",
        creators=[("Niels", "Bohr")],
        year="1913",
    )
    doc_path = _create_mock_bundle(cache_dir, "ATT_DRY", "PARENT_DRY", "Old Dry Title")
    original_text = doc_path.read_text(encoding="utf-8")

    main(["--data-dir", str(zotero_dir), "--dry-run"])

    captured = capsys.readouterr().out
    assert "Dry-run complete: 1 item(s) would be updated" in captured
    assert doc_path.read_text(encoding="utf-8") == original_text


def test_cli_specific_item_keys(mock_zotero_environment, capsys):
    """Verify CLI with specific positional item keys only targets requested items."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    _add_zotero_article(
        sqlite_path,
        item_id=31,
        item_key="P_A",
        title="Title A Fresh",
        creators=[("Albert", "Einstein")],
    )
    _add_zotero_article(
        sqlite_path,
        item_id=32,
        item_key="P_B",
        title="Title B Fresh",
        creators=[("Max", "Planck")],
    )

    doc_a = _create_mock_bundle(cache_dir, "ATT_A", "P_A", "Title A Old")
    doc_b = _create_mock_bundle(cache_dir, "ATT_B", "P_B", "Title B Old")

    main(["--data-dir", str(zotero_dir), "ATT_A"])

    captured = capsys.readouterr().out
    assert "Targeting 1 item key(s): ATT_A" in captured
    assert "Frontmatter refresh complete: 1 updated" in captured

    assert "Title A Fresh" in doc_a.read_text(encoding="utf-8")
    assert "Title B Old" in doc_b.read_text(encoding="utf-8")


def test_cli_stale_only(mock_zotero_environment, capsys):
    """Verify CLI --stale-only invokes sync_stale_metadata and updates only stale bundles."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    now = datetime.now(timezone.utc)
    conv_time = (now - timedelta(days=2)).isoformat()
    mod_stale = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    mod_fresh = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    # Stale item
    _add_zotero_article(
        sqlite_path,
        item_id=41,
        item_key="P_STALE",
        title="Title Stale New",
        creators=[("Rosalind", "Franklin")],
        client_date_modified=mod_stale,
    )
    # Fresh item
    _add_zotero_article(
        sqlite_path,
        item_id=42,
        item_key="P_FRESH",
        title="Title Fresh",
        creators=[("James", "Watson")],
        client_date_modified=mod_fresh,
    )

    doc_stale = _create_mock_bundle(cache_dir, "ATT_STALE", "P_STALE", "Title Stale Old", converted_at=conv_time)
    doc_fresh = _create_mock_bundle(cache_dir, "ATT_FRESH", "P_FRESH", "Title Fresh", converted_at=conv_time)

    main(["--data-dir", str(zotero_dir), "--stale-only"])

    captured = capsys.readouterr().out
    assert "Checking for stale metadata across cached bundles" in captured
    assert "Staleness sync complete: 1 updated, 0 skipped, 0 errored (total scanned: 1)." in captured

    assert "Title Stale New" in doc_stale.read_text(encoding="utf-8")
    assert "Title Fresh" in doc_fresh.read_text(encoding="utf-8")

    # Second sweep: everything is now fresh
    main(["--data-dir", str(zotero_dir), "--stale-only"])
    captured_second = capsys.readouterr().out
    assert "Staleness sync complete: 0 updated, 2 skipped, 0 errored (total scanned: 2)." in captured_second


def test_cli_sanitize_images(mock_zotero_environment, capsys):
    """Verify CLI --sanitize-images rewrites legacy absolute file:/// URIs in document.md."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    _add_zotero_article(
        sqlite_path,
        item_id=50,
        item_key="P_IMG",
        title="Image Paper",
        creators=[("Richard", "Feynman")],
    )

    legacy_body = (
        "## Figures\n\n"
        "![Figure 1](file:///home/alex/Zotero/storage/ATT_IMG/images/diag1.png)\n"
    )
    doc_path = _create_mock_bundle(cache_dir, "ATT_IMG", "P_IMG", "Image Paper", extra_body=legacy_body)

    main(["--data-dir", str(zotero_dir), "--sanitize-images", "ATT_IMG"])

    captured = capsys.readouterr().out
    assert "Frontmatter refresh complete: 1 updated" in captured

    refreshed_text = doc_path.read_text(encoding="utf-8")
    assert "images/diag1.png" in refreshed_text
    assert "file://" not in refreshed_text


def test_find_zotero_dir(tmp_path: Path):
    """Verify find_zotero_dir returns valid directory or exits on invalid directory."""
    valid_dir = tmp_path / "valid_zotero"
    valid_dir.mkdir()

    assert find_zotero_dir(str(valid_dir)) == valid_dir

    with pytest.raises(SystemExit):
        find_zotero_dir(str(tmp_path / "non_existent_dir"))


def test_cli_stale_only_dry_run(mock_zotero_environment, capsys):
    """Verify CLI --stale-only --dry-run previews stale changes without modifying disk."""
    zotero_dir, cache_dir, sqlite_path = mock_zotero_environment

    now = datetime.now(timezone.utc)
    conv_time = (now - timedelta(days=2)).isoformat()
    mod_stale = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    _add_zotero_article(
        sqlite_path,
        item_id=71,
        item_key="P_STALE_DRY",
        title="Stale Title Modified",
        creators=[("Barbara", "McClintock")],
        client_date_modified=mod_stale,
    )

    doc_path = _create_mock_bundle(cache_dir, "ATT_STALE_DRY", "P_STALE_DRY", "Stale Title Old", converted_at=conv_time)
    original_text = doc_path.read_text(encoding="utf-8")

    main(["--data-dir", str(zotero_dir), "--stale-only", "--dry-run"])

    captured = capsys.readouterr().out
    assert "Dry-run complete: 1 item(s) would be updated" in captured
    assert doc_path.read_text(encoding="utf-8") == original_text


def test_cli_missing_zotero_sqlite_exits(tmp_path: Path, capsys):
    """Verify CLI exits with code 1 when zotero.sqlite does not exist."""
    empty_zotero = tmp_path / "empty_zotero"
    empty_zotero.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        main(["--data-dir", str(empty_zotero)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr().out
    assert "❌ Error: Zotero database not found" in captured


def test_cli_missing_conversion_meta_exits(tmp_path: Path, capsys):
    """Verify CLI exits with code 1 when conversion_meta.db does not exist."""
    zotero_dir = tmp_path / "zotero_no_meta"
    zotero_dir.mkdir()
    (zotero_dir / "zotero.sqlite").touch()

    with pytest.raises(SystemExit) as exc_info:
        main(["--data-dir", str(zotero_dir)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr().out
    assert "❌ Error: No conversion_meta.db found" in captured
