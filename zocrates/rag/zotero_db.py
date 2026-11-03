"""
Read-only access layer for the Zotero SQLite database.

All queries use a URI connection opened in read-only mode to guarantee we
never accidentally write to the user's library.
"""

import atexit
from contextlib import contextmanager
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_PUBLICATION_FIELDS = (
    "publicationTitle",
    "proceedingsTitle",
    "bookTitle",
    "repository",
    "websiteTitle",
    "blogTitle",
    "publisher",
    "institution",
    "university",
    "encyclopediaTitle",
    "dictionaryTitle",
)

# Batch size for SQLite queries with WHERE IN (...) clauses to respect variable parameter limits
_SQL_BATCH_SIZE: int = 500


def normalize_doi(doi: str | None) -> str | None:
    """Normalize DOI by stripping standard URI and resolver prefixes and lowercasing."""
    if not doi:
        return None
    d = doi.strip()
    prefixes = (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi.org/",
        "doi:",
    )
    for prefix in prefixes:
        if d.lower().startswith(prefix):
            d = d[len(prefix) :].strip()
    d = d.strip().rstrip("/")
    return d.lower() if d else None


ARXIV_ID_PATTERN = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?\/\d{7})$"
)


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Normalize an arXiv identifier by stripping URL prefixes, 'arxiv:' tags, and version suffixes."""
    if not arxiv_id:
        return None
    s = arxiv_id.strip()
    url_m = re.search(
        r"(?i)arxiv\.org\/(?:abs|pdf)\/([a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?\/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)",
        s,
    )
    if url_m:
        s = url_m.group(1)
    else:
        doi_m = re.search(
            r"(?i)10\.48550\/arxiv\.([a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?\/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)",
            s,
        )
        if doi_m:
            s = doi_m.group(1)
        else:
            if s.lower().startswith("arxiv:"):
                s = s[6:].strip()

    s = re.sub(r"(?i)v\d+$", "", s).strip()
    s = re.sub(r"(?i)\.pdf$", "", s).strip().rstrip(".")
    s = s.lower()
    if ARXIV_ID_PATTERN.match(s):
        return s
    return None


def extract_arxiv_id(
    extra: str | None = None,
    url: str | None = None,
    doi: str | None = None,
) -> str | None:
    """Extract and normalize an arXiv ID from extra text, URL, or DOI."""
    if extra:
        m = re.search(
            r"(?i)\barxiv:\s*([a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?\/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)",
            extra,
        )
        if m:
            return normalize_arxiv_id(m.group(1))
        m_fallback = re.search(r"(?i)\barxiv:\s*(\S+)", extra)
        if m_fallback:
            cand = normalize_arxiv_id(m_fallback.group(1))
            if cand:
                return cand

    if url:
        cand = normalize_arxiv_id(url)
        if cand:
            return cand

    if doi:
        cand = normalize_arxiv_id(doi)
        if cand:
            return cand

    return None


def _parse_utc_datetime(ts_str: str | None) -> datetime | None:
    """Parse a date string into a timezone-aware UTC datetime."""
    if not ts_str:
        return None
    try:
        ts_clean = ts_str.strip()
        if ts_clean.endswith("Z"):
            ts_clean = ts_clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def extract_year(date_val: str | None) -> str | None:
    """Extract a 4-digit year string from a Zotero date value."""
    if not date_val:
        return None
    s = date_val.strip()
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", s)
    if m:
        return m.group(1)
    if len(s) >= 4 and s[:4].isdigit():
        return s[:4]
    return None


@dataclass
class ZoteroCollection:
    """Representation of a Zotero collection."""

    name: str
    key: str


@dataclass
class ZoteroItem:
    """Lightweight representation of a Zotero library item."""

    item_id: int
    item_key: str
    item_type: str
    title: str
    creators: list[str] = field(default_factory=list)
    year: str | None = None
    publication: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    tags: list[str] = field(default_factory=list)
    collections: list[ZoteroCollection] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    arxiv_id: str | None = None


@dataclass
class ZoteroAttachment:
    """An attachment (PDF, HTML, etc.) linked to a Zotero item."""

    item_id: int
    item_key: str
    parent_item_id: int | None
    parent_item_key: str | None
    content_type: str
    path: Path | None
    title: str = ""


@dataclass
class ZoteroNote:
    """A Zotero note item (standalone or attached to a parent item)."""

    note_id: int
    note_key: str
    parent_item_id: int | None
    parent_item_key: str | None
    title: str
    note_html: str


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _get_connection(sqlite_path: Path) -> sqlite3.Connection:
    """Resolve and open a raw SQLite connection to the Zotero database."""
    with _sqlite_snapshot_lock:
        cached = _sqlite_snapshots.get(str(sqlite_path))
        if cached is not None and os.path.exists(cached[0]):
            try:
                sig = _sqlite_snapshot_signature(sqlite_path)
                if cached[1] == sig:
                    conn = sqlite3.connect(cached[0], timeout=1.0)
                    conn.row_factory = sqlite3.Row
                    return conn
            except (OSError, sqlite3.Error):
                pass

    from urllib.parse import quote

    encoded_path = quote(str(sqlite_path), safe="/:")
    try:
        conn = sqlite3.connect(
            f"file:{encoded_path}?mode=ro", uri=True, timeout=0.1
        )
        conn.row_factory = sqlite3.Row
        # Probe the read: a mode=ro connection that succeeds but cannot
        # query (edge lock states) should fall through to the snapshot tier
        # instead of raising deep inside a sync pipeline.
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            conn.close()
    except sqlite3.Error:
        pass
    try:
        return _open_snapshot_connection(sqlite_path)
    except (OSError, sqlite3.Error):
        pass
    uri = f"file:{encoded_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _open_db(sqlite_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the Zotero SQLite DB with a read-correct, lock-safe strategy.

    Tiered connection strategy (WAL-correct as of the fresh-profile fix):

    1. ``mode=ro``: the correct view whenever SQLite can take the shared
       read path — Zotero running in WAL mode (readers never block writers
       under WAL), or a cleanly closed database. A read-only connection
       sees committed WAL frames, so un-checkpointed data is visible.
    2. Snapshot copy: when ``mode=ro`` fails (e.g. an unclean Zotero
       shutdown leaves an un-checkpointed ``-wal`` sidecar with no ``-shm``
       — a read-only connection cannot recover the WAL and reports
       "database is locked"), the DB plus its ``-wal`` / ``-journal``
       sidecars are copied to a private temp file and opened read-write so
       SQLite performs WAL recovery on the copy. The snapshot is cached per
       source path and invalidated whenever the main file mtime or WAL size
       changes (static while Zotero is closed, so the copy cost is paid at
       most once per Zotero session state).
    3. ``immutable=1`` last resort (legacy behavior): bypasses OS locks but
       silently ignores WAL journals — only reached when both strategies
       above fail, e.g. truly read-only filesystems.

    ``immutable=1`` alone is NOT sufficient in general: Zotero writes into
    ``zotero.sqlite-wal`` and only merges into the main file on checkpoint
    (typically at shutdown), so a fresh session's committed items can exist
    exclusively in the WAL. Reading the main file with ``immutable=1`` then
    reports an empty library even though the user sees items in Zotero.

    The file path is URL-encoded because Zotero data directories may contain
    spaces or special characters that break SQLite URI parameter parsing.

    Must be used as a context manager (``with _open_db(path) as conn:``).
    The connection and OS file locks are deterministically closed on exit.
    """
    conn = _get_connection(sqlite_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshot-copy connection cache (WAL recovery fallback)
# ---------------------------------------------------------------------------

_sqlite_snapshot_lock = threading.Lock()
# source path -> (temp DB path, (main mtime_ns, wal size)) signature
_sqlite_snapshots: dict[str, tuple[str, tuple[int, int]]] = {}


def _sqlite_snapshot_signature(sqlite_path: Path) -> tuple[int, int, int]:
    """Signature used to invalidate cached snapshots: main mtime + WAL size + WAL mtime."""
    st = sqlite_path.stat()
    wal_path = Path(str(sqlite_path) + "-wal")
    if wal_path.exists():
        wal_st = wal_path.stat()
        wal_size = wal_st.st_size
        wal_mtime = wal_st.st_mtime_ns
    else:
        wal_size = 0
        wal_mtime = 0
    return (st.st_mtime_ns, wal_size, wal_mtime)


def _cleanup_sqlite_snapshots() -> None:
    """Remove all cached snapshot temp files (registered via atexit)."""
    for tmp_path, _sig in list(_sqlite_snapshots.values()):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(tmp_path + suffix)
            except OSError:
                pass
    _sqlite_snapshots.clear()


atexit.register(_cleanup_sqlite_snapshots)


def _open_snapshot_connection(sqlite_path: Path) -> sqlite3.Connection:
    """Open a private recovered copy of the Zotero DB (WAL fallback tier).

    Copies the main DB plus ``-wal`` / ``-journal`` sidecars into a private
    temp directory and opens the copy read-write, letting SQLite recover
    committed WAL frames on open. Cached per source path until the main
    file mtime or WAL size changes (the fallback regime is by definition a
    static, Zotero-closed state, so the copy cost is paid once).
    """
    with _sqlite_snapshot_lock:
        sig = _sqlite_snapshot_signature(sqlite_path)
        cached = _sqlite_snapshots.get(str(sqlite_path))
        if cached is not None and cached[1] == sig:
            conn = sqlite3.connect(cached[0], timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
        tmp_dir = tempfile.mkdtemp(prefix="zocrates-zdb-")
        tmp_path = os.path.join(tmp_dir, "zotero.sqlite")
        shutil.copy2(sqlite_path, tmp_path)
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(sqlite_path) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, tmp_path + suffix)
        if cached is not None:
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.remove(cached[0] + suffix)
                except OSError:
                    pass
        conn = sqlite3.connect(tmp_path, timeout=5)
        conn.row_factory = sqlite3.Row
        _sqlite_snapshots[str(sqlite_path)] = (tmp_path, sig)
        return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ZoteroDB:
    """Read-only façade over zotero.sqlite."""

    def __init__(self, sqlite_path: Path, storage_dir: Path) -> None:
        """Initialize ZoteroDB.

        Args:
            sqlite_path: Absolute path to the ``zotero.sqlite`` database file.
            storage_dir: Absolute path to the Zotero ``storage/`` directory,
                used to resolve relative attachment paths.
        """
        self._sqlite_path = sqlite_path
        self._storage_dir = storage_dir  # <ZoteroDataDir>/storage/
        self._table_exists_cache: dict[str, bool] = {}

    def invalidate_schema_cache(self) -> None:
        """Clear cached table existence checks to re-probe schema on next access."""
        self._table_exists_cache.clear()

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        """Check if a table exists in the database, caching the result on self."""
        if table_name in self._table_exists_cache:
            return self._table_exists_cache[table_name]
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        exists = bool(row)
        self._table_exists_cache[table_name] = exists
        return exists

    # ---- items -------------------------------------------------------------

    def get_all_items(self) -> list[ZoteroItem]:
        """Return all non-attachment, non-deleted items in the library.

        Returns:
            List of fully populated ``ZoteroItem`` objects.
        """
        with _open_db(self._sqlite_path) as conn:
            has_deleted = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted else ""
            sql = f"""
                SELECT
                    i.itemID,
                    i.key,
                    it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE it.typeName != 'attachment'
                  AND it.typeName != 'note'
                  {del_clause}
                ORDER BY i.itemID
            """
            rows = conn.execute(sql).fetchall()
            return self._build_items_batched(conn, rows)

    def get_item(self, item_id: int) -> ZoteroItem | None:
        """Fetch a single item with full metadata.

        Args:
            item_id: Numeric Zotero item ID.

        Returns:
            Fully populated ``ZoteroItem`` (with creators, tags, collections,
            and relations), or ``None`` if not found or deleted.
        """
        with _open_db(self._sqlite_path) as conn:
            has_deleted = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted else ""
            sql = f"""
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE i.itemID = ?
                  {del_clause}
            """
            row = conn.execute(sql, (item_id,)).fetchone()
            if row is None:
                return None
            return self._build_item(conn, row)

    def get_item_by_key(self, item_key: str) -> ZoteroItem | None:
        """Fetch a single item by its string key.

        Args:
            item_key: Zotero item key string (e.g. '89X2ELQ6').

        Returns:
            Fully populated ``ZoteroItem`` or ``None`` if not found.
        """
        with _open_db(self._sqlite_path) as conn:
            has_deleted = self._table_exists(conn, "deletedItems")
            del_clause = "AND itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted else ""
            sql = f"""
                SELECT itemID FROM items WHERE key = ? {del_clause}
            """
            row = conn.execute(sql, (item_key,)).fetchone()
            if row is None:
                return None
            return self.get_item(row["itemID"])

    def get_active_item_keys(self, item_keys: list[str]) -> set[str]:
        """Return a set of item keys that exist in the library and are not deleted or parent-deleted.

        Args:
            item_keys: List of Zotero item keys (can include regular items, attachments, notes).

        Returns:
            Set of active item keys.
        """
        if not item_keys:
            return set()

        unique_keys = list(dict.fromkeys(k for k in item_keys if k))
        if not unique_keys:
            return set()

        active: set[str] = set()
        batch_size = _SQL_BATCH_SIZE

        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            has_item_attachments = self._table_exists(conn, "itemAttachments")
            has_item_notes = self._table_exists(conn, "itemNotes")

            del_clause = ""
            if has_deleted_items:
                del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)"
                if has_item_attachments:
                    del_clause += " AND (ia.parentItemID IS NULL OR ia.parentItemID NOT IN (SELECT itemID FROM deletedItems))"
                if has_item_notes:
                    del_clause += " AND (n.parentItemID IS NULL OR n.parentItemID NOT IN (SELECT itemID FROM deletedItems))"

            join_attachments = (
                "LEFT JOIN itemAttachments ia ON i.itemID = ia.itemID"
                if has_item_attachments
                else ""
            )
            join_notes = (
                "LEFT JOIN itemNotes n ON i.itemID = n.itemID"
                if has_item_notes
                else ""
            )

            for i in range(0, len(unique_keys), batch_size):
                batch = unique_keys[i : i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                sql = f"""
                    SELECT i.key
                    FROM items i
                    {join_attachments}
                    {join_notes}
                    WHERE i.key IN ({placeholders})
                      {del_clause}
                """
                rows = conn.execute(sql, batch).fetchall()
                for row in rows:
                    if row["key"]:
                        active.add(row["key"])

        return active

    def is_item_active(self, item_key: str) -> bool:
        """Check if a single item key exists and is not deleted (or parent-deleted).

        Args:
            item_key: Zotero item key.

        Returns:
            True if the item exists and is active, False otherwise.
        """
        if not item_key:
            return False
        return item_key in self.get_active_item_keys([item_key])

    def get_items_by_ids(self, item_ids: list[int]) -> list[ZoteroItem]:
        """Fetch multiple items by their numeric IDs.

        Args:
            item_ids: List of numeric Zotero item IDs.

        Returns:
            List of fully populated ``ZoteroItem`` objects (with creators,
            tags, and collections). Items not found or deleted are silently
            omitted.
        """
        if not item_ids:
            return []
        batch_size = _SQL_BATCH_SIZE
        with _open_db(self._sqlite_path) as conn:
            has_deleted = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted else ""
            all_rows: list[sqlite3.Row] = []
            for i in range(0, len(item_ids), batch_size):
                batch_chunk = item_ids[i : i + batch_size]
                placeholders = ",".join("?" for _ in batch_chunk)
                sql = f"""
                    SELECT i.itemID, i.key, it.typeName
                    FROM items i
                    JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                    WHERE i.itemID IN ({placeholders})
                      {del_clause}
                """
                all_rows.extend(conn.execute(sql, batch_chunk).fetchall())
            return self._build_items_batched(conn, all_rows)

    def get_new_item_ids_since(self, last_seen_item_id: int) -> list[int]:
        """Return item IDs created after last_seen_item_id for incremental sync.

        Args:
            last_seen_item_id: The highest item ID already processed.

        Returns:
            Sorted list of numeric item IDs greater than ``last_seen_item_id``
            that are not attachments, notes, or deleted items.
        """
        sql = """
            SELECT i.itemID
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE it.typeName != 'attachment'
              AND it.typeName != 'note'
              AND i.itemID > ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.itemID
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql, (last_seen_item_id,)).fetchall()
        return [row["itemID"] for row in rows]

    def get_item_date_modified(self, item_key: str) -> datetime | None:
        """Fetch the last modified timestamp for an item as timezone-aware UTC datetime.

        Args:
            item_key: 8-character Zotero alphanumeric key.

        Returns:
            Timezone-aware UTC datetime, or None if item not found.
        """
        sql = """
            SELECT COALESCE(clientDateModified, dateModified) AS modified
            FROM items
            WHERE key = ?
              AND itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            row = conn.execute(sql, (item_key,)).fetchone()
            if row and row["modified"]:
                return _parse_utc_datetime(row["modified"])
        return None

    def get_stale_item_keys(
        self,
        converted_map: dict[str, str],
        key_to_atts: dict[str, list[str]],
    ) -> list[str]:
        """Identify cached items whose Zotero metadata has been modified since conversion.

        Args:
            converted_map: Mapping of attachment/note item_key -> converted_at ISO string.
            key_to_atts: Mapping of query key (parent_item_key or direct item_key)
                to list of associated attachment/note item_keys.

        Returns:
            Sorted, unique list of attachment/note item_keys whose metadata is stale.
        """
        if not key_to_atts or not converted_map:
            return []

        stale_item_keys: set[str] = set()
        query_keys = list(key_to_atts.keys())
        batch_size = 500

        with _open_db(self._sqlite_path) as conn:
            for i in range(0, len(query_keys), batch_size):
                batch = query_keys[i : i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                sql = f"""
                    SELECT key, COALESCE(clientDateModified, dateModified) AS modified
                    FROM items
                    WHERE key IN ({placeholders})
                      AND itemID NOT IN (SELECT itemID FROM deletedItems)
                """
                rows = conn.execute(sql, batch).fetchall()
                for row in rows:
                    q_key = row["key"]
                    mod_str = row["modified"]
                    if not mod_str:
                        continue
                    mod_dt = _parse_utc_datetime(mod_str)
                    if not mod_dt:
                        continue

                    for att_key in key_to_atts.get(q_key, []):
                        conv_str = converted_map.get(att_key)
                        if not conv_str:
                            continue
                        conv_dt = _parse_utc_datetime(conv_str)
                        if not conv_dt:
                            continue

                        if mod_dt > conv_dt:
                            stale_item_keys.add(att_key)

        return sorted(stale_item_keys)

    # ---- attachments -------------------------------------------------------

    def get_attachments(
        self,
        parent_item_id: int,
        content_types: tuple[str, ...] = ("application/pdf", "text/html", "text/markdown", "text/plain"),
    ) -> list[ZoteroAttachment]:
        """Return file attachments for a given parent item.

        Args:
            parent_item_id: Numeric ID of the parent Zotero item.
            content_types: Tuple of MIME types to include; defaults to PDF,
                HTML, Markdown, and plain text.

        Returns:
            List of ``ZoteroAttachment`` objects with resolved filesystem
            paths (``path`` is ``None`` if the file cannot be resolved).
            The ``title`` field is set to the Zotero attachment title if
            available, otherwise the attachment filename.
        """
        sql = """
            SELECT
                i.itemID,
                i.key,
                ia.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(ia.contentType, '') AS contentType,
                COALESCE(ia.path, '') AS path,
                COALESCE(att_meta.att_title, '') AS attachmentTitle
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            LEFT JOIN (
                SELECT id2.itemID, idv2.value AS att_title
                FROM itemData id2
                JOIN fields f2 ON id2.fieldID = f2.fieldID
                JOIN itemDataValues idv2 ON id2.valueID = idv2.valueID
                WHERE f2.fieldName = 'title'
            ) att_meta ON i.itemID = att_meta.itemID
            WHERE ia.parentItemID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql, (parent_item_id,)).fetchall()

        attachments: list[ZoteroAttachment] = []
        for row in rows:
            if row["contentType"] not in content_types:
                continue
            resolved = self._resolve_attachment_path(row["key"], row["path"])
            raw_title = (row["attachmentTitle"] or "").strip()
            filename = (resolved.name if resolved and resolved.name else "")
            if raw_title and filename and raw_title.lower() != filename.lower():
                att_title = f"{raw_title} ({filename})"
            elif raw_title:
                att_title = raw_title
            elif filename:
                att_title = filename
            else:
                att_title = "Attachment"
            attachments.append(
                ZoteroAttachment(
                    item_id=row["itemID"],
                    item_key=row["key"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    content_type=row["contentType"],
                    path=resolved,
                    title=att_title,
                )
            )
        return attachments

    def get_all_attachments(
        self,
        content_types: tuple[str, ...] = ("application/pdf", "text/html", "text/markdown", "text/plain"),
    ) -> list[ZoteroAttachment]:
        """Return all file attachments with a parent item across the library.

        Args:
            content_types: Tuple of MIME types to include.

        Returns:
            List of ``ZoteroAttachment`` objects with resolved filesystem paths.
        """
        sql = """
            SELECT
                i.itemID,
                i.key,
                ia.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(ia.contentType, '') AS contentType,
                COALESCE(ia.path, '') AS path
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND ia.parentItemID IS NOT NULL
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql).fetchall()

        attachments: list[ZoteroAttachment] = []
        for row in rows:
            if row["contentType"] not in content_types:
                continue
            resolved = self._resolve_attachment_path(row["key"], row["path"])
            attachments.append(
                ZoteroAttachment(
                    item_id=row["itemID"],
                    item_key=row["key"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    content_type=row["contentType"],
                    path=resolved,
                )
            )
        return attachments

    def get_standalone_attachments(
        self,
        content_types: tuple[str, ...] = ("application/pdf", "text/html", "text/markdown", "text/plain"),
    ) -> list[ZoteroAttachment]:
        """Return standalone file attachments (attachments without a parent item).

        Args:
            content_types: Tuple of MIME types to include.

        Returns:
            List of ``ZoteroAttachment`` objects whose ``parent_item_id`` is ``None``.
        """
        sql = """
            SELECT
                i.itemID,
                i.key,
                ia.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(ia.contentType, '') AS contentType,
                COALESCE(ia.path, '') AS path
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            WHERE ia.parentItemID IS NULL
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql).fetchall()

        attachments: list[ZoteroAttachment] = []
        for row in rows:
            if row["contentType"] not in content_types:
                continue
            resolved = self._resolve_attachment_path(row["key"], row["path"])
            attachments.append(
                ZoteroAttachment(
                    item_id=row["itemID"],
                    item_key=row["key"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    content_type=row["contentType"],
                    path=resolved,
                )
            )
        return attachments

    def get_all_convertible_attachments(self) -> list[dict[str, Any]]:
        """Return all valid document attachments in the library in a single fast query."""
        sql = """
            SELECT
                i.itemID AS attachment_id,
                i.key AS attachment_key,
                ia.parentItemID AS parent_item_id,
                parentItem.key AS parent_key,
                COALESCE(idvTitle.value, '') AS parent_title,
                COALESCE(ia.contentType, '') AS content_type,
                COALESCE(ia.path, '') AS raw_path
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            LEFT JOIN itemData idTitle ON parentItem.itemID = idTitle.itemID AND idTitle.fieldID = (
                SELECT fieldID FROM fields WHERE fieldName = 'title'
            )
            LEFT JOIN itemDataValues idvTitle ON idTitle.valueID = idvTitle.valueID
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND (parentItem.itemID IS NULL OR parentItem.itemID NOT IN (SELECT itemID FROM deletedItems))
            ORDER BY i.itemID
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            resolved_path = self._resolve_attachment_path(row["attachment_key"], row["raw_path"])
            if resolved_path and resolved_path.exists():
                results.append({
                    "attachment_id": row["attachment_id"],
                    "attachment_key": row["attachment_key"],
                    "parent_item_id": row["parent_item_id"],
                    "parent_key": row["parent_key"] or "",
                    "parent_title": row["parent_title"] or "",
                    "content_type": row["content_type"],
                    "path": resolved_path,
                    "filename": resolved_path.name,
                })
        return results

    def get_attachment_by_id(
        self,
        item_id: int,
    ) -> ZoteroAttachment | None:
        """Fetch a single file attachment by its item ID.

        Args:
            item_id: Numeric Zotero item ID of the attachment.

        Returns:
            ``ZoteroAttachment`` with a resolved path, or ``None`` if not found
            or deleted.
        """
        sql = """
            SELECT
                i.itemID,
                i.key,
                ia.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(ia.contentType, '') AS contentType,
                COALESCE(ia.path, '') AS path
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            WHERE i.itemID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            row = conn.execute(sql, (item_id,)).fetchone()
            if row is None:
                return None
            resolved = self._resolve_attachment_path(row["key"], row["path"])
            return ZoteroAttachment(
                item_id=row["itemID"],
                item_key=row["key"],
                parent_item_id=row["parentItemID"],
                parent_item_key=row["parentKey"],
                content_type=row["contentType"],
                path=resolved,
            )

    def get_attachment_by_key(
        self,
        item_key: str,
    ) -> ZoteroAttachment | None:
        """Fetch a single file attachment by its item key or parent item key.

        Args:
            item_key: Zotero item key string (attachment key or parent item key).

        Returns:
            ``ZoteroAttachment`` with a resolved path, or ``None`` if not found.
        """
        sql = """
            SELECT
                i.itemID,
                i.key,
                ia.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(ia.contentType, '') AS contentType,
                COALESCE(ia.path, '') AS path
            FROM items i
            JOIN itemAttachments ia ON i.itemID = ia.itemID
            LEFT JOIN items parentItem ON ia.parentItemID = parentItem.itemID
            WHERE (i.key = ? OR parentItem.key = ?)
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY CASE WHEN ia.contentType = 'application/pdf' THEN 0 ELSE 1 END
        """
        with _open_db(self._sqlite_path) as conn:
            row = conn.execute(sql, (item_key, item_key)).fetchone()
            if row:
                resolved = self._resolve_attachment_path(row["key"], row["path"])
                return ZoteroAttachment(
                    item_id=row["itemID"],
                    item_key=row["key"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    content_type=row["contentType"],
                    path=resolved,
                )

        item = self.get_item_by_key(item_key)
        if item:
            attachments = self.get_attachments(item.item_id)
            if attachments:
                return sorted(
                    attachments,
                    key=lambda a: 0 if a.content_type == "application/pdf" else 1,
                )[0]

        return None


    # ---- notes -------------------------------------------------------------

    def get_all_notes(self) -> list[ZoteroNote]:
        """Return all notes (standalone and child notes) in the library.

        Returns:
            List of ``ZoteroNote`` objects ordered by ``note_id``.
        """
        sql = """
            SELECT
                i.itemID AS noteID,
                i.key AS noteKey,
                n.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(n.note, '') AS noteHtml,
                COALESCE(n.title, '') AS noteTitle
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemNotes n ON i.itemID = n.itemID
            LEFT JOIN items parentItem ON n.parentItemID = parentItem.itemID
            WHERE it.typeName = 'note'
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.itemID
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql).fetchall()

        notes: list[ZoteroNote] = []
        for row in rows:
            notes.append(
                ZoteroNote(
                    note_id=row["noteID"],
                    note_key=row["noteKey"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    title=row["noteTitle"],
                    note_html=row["noteHtml"],
                )
            )
        return notes

    def get_standalone_notes(self) -> list[ZoteroNote]:
        """Return top-level standalone notes (notes without a parent item).

        Returns:
            List of ``ZoteroNote`` objects whose ``parent_item_id`` is ``None``.
        """
        sql = """
            SELECT
                i.itemID AS noteID,
                i.key AS noteKey,
                n.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(n.note, '') AS noteHtml,
                COALESCE(n.title, '') AS noteTitle
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemNotes n ON i.itemID = n.itemID
            LEFT JOIN items parentItem ON n.parentItemID = parentItem.itemID
            WHERE it.typeName = 'note'
              AND n.parentItemID IS NULL
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.itemID
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql).fetchall()

        notes: list[ZoteroNote] = []
        for row in rows:
            notes.append(
                ZoteroNote(
                    note_id=row["noteID"],
                    note_key=row["noteKey"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    title=row["noteTitle"],
                    note_html=row["noteHtml"],
                )
            )
        return notes

    def get_notes_for_item(self, parent_item_id: int) -> list[ZoteroNote]:
        """Return child notes attached to a specific parent item.

        Args:
            parent_item_id: Numeric ID of the parent Zotero item.

        Returns:
            List of ``ZoteroNote`` objects whose ``parent_item_id`` matches.
        """
        sql = """
            SELECT
                i.itemID AS noteID,
                i.key AS noteKey,
                n.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(n.note, '') AS noteHtml,
                COALESCE(n.title, '') AS noteTitle
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemNotes n ON i.itemID = n.itemID
            LEFT JOIN items parentItem ON n.parentItemID = parentItem.itemID
            WHERE it.typeName = 'note'
              AND n.parentItemID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.itemID
        """
        with _open_db(self._sqlite_path) as conn:
            rows = conn.execute(sql, (parent_item_id,)).fetchall()

        notes: list[ZoteroNote] = []
        for row in rows:
            notes.append(
                ZoteroNote(
                    note_id=row["noteID"],
                    note_key=row["noteKey"],
                    parent_item_id=row["parentItemID"],
                    parent_item_key=row["parentKey"],
                    title=row["noteTitle"],
                    note_html=row["noteHtml"],
                )
            )
        return notes

    def get_note_by_id(self, note_id: int) -> ZoteroNote | None:
        """Fetch a single note by note item ID."""
        sql = """
            SELECT
                i.itemID AS noteID,
                i.key AS noteKey,
                n.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(n.note, '') AS noteHtml,
                COALESCE(n.title, '') AS noteTitle
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemNotes n ON i.itemID = n.itemID
            LEFT JOIN items parentItem ON n.parentItemID = parentItem.itemID
            WHERE i.itemID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            row = conn.execute(sql, (note_id,)).fetchone()
            if row is None:
                return None
            return ZoteroNote(
                note_id=row["noteID"],
                note_key=row["noteKey"],
                parent_item_id=row["parentItemID"],
                parent_item_key=row["parentKey"],
                title=row["noteTitle"],
                note_html=row["noteHtml"],
            )

    def get_note_by_key(self, note_key: str) -> "ZoteroNote | None":
        """Fetch a single note by its Zotero item key.

        Args:
            note_key: The alphanumeric Zotero item key of the note.

        Returns:
            A ``ZoteroNote`` if found, or ``None`` if the key does not exist
            or the item has been deleted.
        """
        sql = """
            SELECT
                i.itemID AS noteID,
                i.key AS noteKey,
                n.parentItemID,
                parentItem.key AS parentKey,
                COALESCE(n.note, '') AS noteHtml,
                COALESCE(n.title, '') AS noteTitle
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemNotes n ON i.itemID = n.itemID
            LEFT JOIN items parentItem ON n.parentItemID = parentItem.itemID
            WHERE i.key = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        with _open_db(self._sqlite_path) as conn:
            row = conn.execute(sql, (note_key,)).fetchone()
            if row is None:
                return None
            return ZoteroNote(
                note_id=row["noteID"],
                note_key=row["noteKey"],
                parent_item_id=row["parentItemID"],
                parent_item_key=row["parentKey"],
                title=row["noteTitle"],
                note_html=row["noteHtml"],
            )

    def get_all_dois(self) -> set[str]:
        """Return a set of all normalized DOIs across active non-deleted items in the library."""
        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT idv.value
                FROM items i
                JOIN itemData id ON i.itemID = id.itemID
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE f.fieldName = 'DOI'
                  {del_clause}
            """
            rows = conn.execute(sql).fetchall()
            dois: set[str] = set()
            for r in rows:
                norm = normalize_doi(r["value"])
                if norm:
                    dois.add(norm)
            return dois

    def get_all_arxiv_ids(self) -> set[str]:
        """Return a set of all normalized arXiv IDs across active non-deleted items in the library."""
        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT f.fieldName, idv.value
                FROM items i
                JOIN itemData id ON i.itemID = id.itemID
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE f.fieldName IN ('extra', 'url', 'DOI')
                  {del_clause}
            """
            rows = conn.execute(sql).fetchall()
            arxiv_ids: set[str] = set()
            for r in rows:
                field_name = r["fieldName"]
                val = r["value"]
                cand: str | None = None
                if field_name == "extra":
                    cand = extract_arxiv_id(extra=val)
                elif field_name == "url":
                    cand = extract_arxiv_id(url=val)
                elif field_name == "DOI":
                    cand = extract_arxiv_id(doi=val)
                if cand:
                    arxiv_ids.add(cand)
            return arxiv_ids

    def get_all_titles(self) -> list[str]:
        """Return a list of all titles across active non-deleted items in the library."""
        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT idv.value
                FROM items i
                JOIN itemData id ON i.itemID = id.itemID
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE f.fieldName = 'title'
                  {del_clause}
            """
            rows = conn.execute(sql).fetchall()
            titles: list[str] = []
            for r in rows:
                t = (r["value"] or "").strip()
                if t:
                    titles.append(t)
            return titles

    def get_all_dedup_records(self) -> list[dict[str, Any]]:
        """Return lightweight records (title, year, creators, doi, arxiv_id) for active items for fuzzy dedup."""
        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT i.itemID, f.fieldName, idv.value
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                JOIN itemData id ON i.itemID = id.itemID
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE it.typeName NOT IN ('attachment', 'note')
                  AND f.fieldName IN ('title', 'date', 'DOI', 'url', 'extra')
                  {del_clause}
                ORDER BY i.itemID
            """
            rows = conn.execute(sql).fetchall()

            records_map: dict[int, dict[str, Any]] = {}
            for r in rows:
                item_id = r["itemID"]
                if item_id not in records_map:
                    records_map[item_id] = {
                        "item_id": item_id,
                        "title": None,
                        "year": None,
                        "doi": None,
                        "url": None,
                        "extra": None,
                    }
                field_name = r["fieldName"]
                val = r["value"]
                if field_name == "title":
                    records_map[item_id]["title"] = val
                elif field_name == "date":
                    y_str = extract_year(val)
                    if y_str:
                        try:
                            records_map[item_id]["year"] = int(y_str)
                        except ValueError:
                            pass
                elif field_name == "DOI":
                    records_map[item_id]["doi"] = normalize_doi(val)
                elif field_name == "url":
                    records_map[item_id]["url"] = val
                elif field_name == "extra":
                    records_map[item_id]["extra"] = val

            sql_creators = f"""
                SELECT ic.itemID, c.lastName
                FROM itemCreators ic
                JOIN creators c ON ic.creatorID = c.creatorID
                JOIN items i ON ic.itemID = i.itemID
                WHERE 1=1 {del_clause}
                ORDER BY ic.itemID, ic.orderIndex
            """
            c_rows = conn.execute(sql_creators).fetchall()
            creators_map: dict[int, list[str]] = {}
            for cr in c_rows:
                i_id = cr["itemID"]
                lname = (cr["lastName"] or "").strip()
                if lname:
                    creators_map.setdefault(i_id, []).append(lname)

            results: list[dict[str, Any]] = []
            for item_id, rec in records_map.items():
                title = rec["title"]
                if not title:
                    continue
                arxiv_id = extract_arxiv_id(extra=rec["extra"], url=rec["url"], doi=rec["doi"])
                results.append({
                    "item_id": item_id,
                    "title": title,
                    "year": rec["year"],
                    "doi": rec["doi"],
                    "arxiv_id": arxiv_id,
                    "creators": creators_map.get(item_id, []),
                })
            return results

    # ---- private helpers ---------------------------------------------------

    def _resolve_attachment_path(self, item_key: str, raw_path: str) -> Path | None:
        """
        Convert a Zotero attachment path to an absolute filesystem path.

        Zotero stores paths as either:
        - "storage:filename.pdf"  → <storage>/<item_key>/filename.pdf
        - An absolute path
        """
        if not raw_path:
            # Linked-file attachment without a stored path
            return self._storage_dir / item_key  # best-effort

        base_dir = self._storage_dir / item_key

        if raw_path.startswith("storage:"):
            raw_filename = raw_path[len("storage:") :]
            filename = Path(raw_filename).name
            if not filename or filename in (".", "..") or filename != raw_filename:
                logger.warning(
                    "Blocked path traversal attempt in attachment path %r for item %s",
                    raw_path,
                    item_key,
                )
                return None
            return base_dir / filename

        # Absolute or relative path
        p = Path(raw_path)
        if p.is_absolute():
            return p

        # Relative path — enforce lexical containment within base_dir to support
        # valid symlinked storage targets while rejecting traversal sequences.
        norm_base = os.path.normpath(str(base_dir))
        norm_target = os.path.normpath(str(base_dir / p))
        if not (norm_target == norm_base or norm_target.startswith(norm_base + os.sep)):
            logger.warning(
                "Blocked path traversal attempt in relative attachment path %r for item %s",
                raw_path,
                item_key,
            )
            return None
        return base_dir / p

    def _populate_fields(self, conn: sqlite3.Connection, item: ZoteroItem) -> None:
        """Fill in common metadata fields for an item.

        Note: retained as the per-item parity baseline for ``_build_items_batched``.
        """
        sql = """
            SELECT f.fieldName, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID = ?
              AND f.fieldName IN (
                  'title', 'date', 'abstractNote', 'DOI', 'url', 'extra',
                  'publicationTitle', 'proceedingsTitle', 'bookTitle',
                  'repository', 'websiteTitle', 'blogTitle', 'publisher',
                  'institution', 'university', 'encyclopediaTitle', 'dictionaryTitle'
              )
        """
        pub_dict: dict[str, str] = {}
        extra_val: str | None = None
        for row in conn.execute(sql, (item.item_id,)).fetchall():
            name, value = row["fieldName"], row["value"]
            if not value:
                continue
            if name == "title":
                item.title = value
            elif name == "date":
                item.year = extract_year(value)
            elif name == "abstractNote":
                item.abstract = value
            elif name == "DOI":
                item.doi = normalize_doi(value)
            elif name == "url":
                item.url = value.strip()
            elif name == "extra":
                extra_val = value.strip()
            elif name in _PUBLICATION_FIELDS:
                pub_dict[name] = value.strip()

        for field_name in _PUBLICATION_FIELDS:
            if field_name in pub_dict and pub_dict[field_name]:
                item.publication = pub_dict[field_name]
                break

        item.arxiv_id = extract_arxiv_id(extra=extra_val, url=item.url, doi=item.doi)

    def _fetch_creators(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Return creator names for item_id, ordered by their index in the Zotero DB.

        Note: retained as the per-item parity baseline for ``_build_items_batched``.

        Args:
            conn: Open SQLite connection to reuse.
            item_id: Numeric Zotero item ID.

        Returns:
            List of creator name strings formatted as ``"LastName, FirstName"``
            (or just ``"LastName"`` for single-name creators), in display order.
        """
        sql = """
            SELECT c.lastName, c.firstName
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
        """
        rows = conn.execute(sql, (item_id,)).fetchall()
        creators: list[str] = []
        for r in rows:
            last = (r["lastName"] or "").strip()
            first = (r["firstName"] or "").strip()
            if last and first:
                creators.append(f"{last}, {first}")
            elif last:
                creators.append(last)
            elif first:
                creators.append(first)
        return creators

    def _fetch_tags(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Return tag names associated with item_id.

        Note: retained as the per-item parity baseline for ``_build_items_batched``.

        Args:
            conn: Open SQLite connection to reuse.
            item_id: Numeric Zotero item ID.

        Returns:
            List of tag name strings.
        """
        sql = """
            SELECT t.name
            FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID = ?
        """
        return [r["name"] for r in conn.execute(sql, (item_id,)).fetchall()]

    def _fetch_collections(
        self, conn: sqlite3.Connection, item_id: int
    ) -> list[ZoteroCollection]:
        """Return collection names and keys that item_id belongs to.

        Note: retained as the per-item parity baseline for ``_build_items_batched``.

        Args:
            conn: Open SQLite connection to reuse.
            item_id: Numeric Zotero item ID.

        Returns:
            List of ZoteroCollection objects containing name and key.
        """
        sql = """
            SELECT col.collectionName, col.key
            FROM collectionItems ci
            JOIN collections col ON ci.collectionID = col.collectionID
            WHERE ci.itemID = ?
        """
        rows = conn.execute(sql, (item_id,)).fetchall()
        result: list[ZoteroCollection] = []
        for r in rows:
            keys = r.keys()
            name = r["collectionName"] if "collectionName" in keys else ""
            key = r["key"] if "key" in keys and r["key"] is not None else ""
            result.append(ZoteroCollection(name=name, key=key))
        return result

    def _build_items_batched(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row | dict[str, Any]],
    ) -> list[ZoteroItem]:
        """Construct and populate ZoteroItem objects in batches to minimize SQL round trips."""
        if not rows:
            return []

        BATCH_SIZE = _SQL_BATCH_SIZE
        items: list[ZoteroItem] = []

        has_item_relations = self._table_exists(conn, "itemRelations")
        has_deleted_items = self._table_exists(conn, "deletedItems")
        has_items = self._table_exists(conn, "items")
        has_item_data = (
            self._table_exists(conn, "itemData")
            and self._table_exists(conn, "fields")
            and self._table_exists(conn, "itemDataValues")
        )
        has_creators = (
            self._table_exists(conn, "itemCreators")
            and self._table_exists(conn, "creators")
        )
        has_item_tags = (
            self._table_exists(conn, "itemTags")
            and self._table_exists(conn, "tags")
        )
        has_collections = (
            self._table_exists(conn, "collectionItems")
            and self._table_exists(conn, "collections")
        )

        del_keys: set[str] = set()
        if has_item_relations and has_deleted_items and has_items:
            del_rows = conn.execute(
                "SELECT key FROM items WHERE itemID IN (SELECT itemID FROM deletedItems)"
            ).fetchall()
            del_keys = {r["key"] for r in del_rows if r["key"]}

        del_clause_relations = (
            "AND ir.itemID NOT IN (SELECT itemID FROM deletedItems)"
            if has_deleted_items
            else ""
        )

        for i in range(0, len(rows), BATCH_SIZE):
            batch_rows = rows[i : i + BATCH_SIZE]
            batch_items: list[ZoteroItem] = []
            items_by_id: dict[int, list[ZoteroItem]] = defaultdict(list)

            for r in batch_rows:
                item = ZoteroItem(
                    item_id=r["itemID"],
                    item_key=r["key"],
                    item_type=r["typeName"],
                    title="",
                )
                batch_items.append(item)
                items_by_id[item.item_id].append(item)

            batch_ids = [r["itemID"] for r in batch_rows]
            placeholders = ",".join("?" for _ in batch_ids)

            # 1. Query fields: itemData, fields, itemDataValues
            if has_item_data:
                sql_fields = f"""
                    SELECT id.itemID, f.fieldName, idv.value
                    FROM itemData id
                    JOIN fields f ON id.fieldID = f.fieldID
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    WHERE id.itemID IN ({placeholders})
                      AND f.fieldName IN (
                          'title', 'date', 'abstractNote', 'DOI', 'url', 'extra',
                          'publicationTitle', 'proceedingsTitle', 'bookTitle',
                          'repository', 'websiteTitle', 'blogTitle', 'publisher',
                          'institution', 'university', 'encyclopediaTitle', 'dictionaryTitle'
                      )
                """
                pub_dicts: dict[int, dict[str, str]] = defaultdict(dict)
                extra_vals: dict[int, str] = {}
                for r in conn.execute(sql_fields, batch_ids).fetchall():
                    item_id = r["itemID"]
                    name, value = r["fieldName"], r["value"]
                    if not value:
                        continue
                    for target_item in items_by_id.get(item_id, []):
                        if name == "title":
                            target_item.title = value
                        elif name == "date":
                            target_item.year = extract_year(value)
                        elif name == "abstractNote":
                            target_item.abstract = value
                        elif name == "DOI":
                            target_item.doi = normalize_doi(value)
                        elif name == "url":
                            target_item.url = value.strip()
                        elif name == "extra":
                            extra_vals[item_id] = value.strip()
                        elif name in _PUBLICATION_FIELDS:
                            pub_dicts[item_id][name] = value.strip()

                for item in batch_items:
                    pub_dict = pub_dicts.get(item.item_id, {})
                    for field_name in _PUBLICATION_FIELDS:
                        if field_name in pub_dict and pub_dict[field_name]:
                            item.publication = pub_dict[field_name]
                            break
                    item.arxiv_id = extract_arxiv_id(
                        extra=extra_vals.get(item.item_id),
                        url=item.url,
                        doi=item.doi,
                    )

            # 2. Query creators: itemCreators, creators
            if has_creators:
                sql_creators = f"""
                    SELECT ic.itemID, c.lastName, c.firstName
                    FROM itemCreators ic
                    JOIN creators c ON ic.creatorID = c.creatorID
                    WHERE ic.itemID IN ({placeholders})
                    ORDER BY ic.itemID, ic.orderIndex
                """
                creators_by_item: dict[int, list[str]] = defaultdict(list)
                for r in conn.execute(sql_creators, batch_ids).fetchall():
                    item_id = r["itemID"]
                    last = (r["lastName"] or "").strip()
                    first = (r["firstName"] or "").strip()
                    if last and first:
                        creators_by_item[item_id].append(f"{last}, {first}")
                    elif last:
                        creators_by_item[item_id].append(last)
                    elif first:
                        creators_by_item[item_id].append(first)

                for item in batch_items:
                    item.creators = list(creators_by_item.get(item.item_id, []))

            # 3. Query tags: itemTags, tags
            if has_item_tags:
                sql_tags = f"""
                    SELECT it.itemID, t.name
                    FROM itemTags it
                    JOIN tags t ON it.tagID = t.tagID
                    WHERE it.itemID IN ({placeholders})
                    ORDER BY it.itemID, it.rowid
                """
                tags_by_item: dict[int, list[str]] = defaultdict(list)
                for r in conn.execute(sql_tags, batch_ids).fetchall():
                    tags_by_item[r["itemID"]].append(r["name"])

                for item in batch_items:
                    item.tags = list(tags_by_item.get(item.item_id, []))

            # 4. Query collections: collectionItems, collections
            if has_collections:
                sql_collections = f"""
                    SELECT ci.itemID, col.collectionName, col.key
                    FROM collectionItems ci
                    JOIN collections col ON ci.collectionID = col.collectionID
                    WHERE ci.itemID IN ({placeholders})
                    ORDER BY ci.itemID, ci.rowid
                """
                collections_by_item: dict[int, list[ZoteroCollection]] = defaultdict(list)
                for r in conn.execute(sql_collections, batch_ids).fetchall():
                    keys = r.keys()
                    name = r["collectionName"] if "collectionName" in keys else ""
                    key = r["key"] if "key" in keys and r["key"] is not None else ""
                    collections_by_item[r["itemID"]].append(ZoteroCollection(name=name, key=key))

                for item in batch_items:
                    item.collections = list(collections_by_item.get(item.item_id, []))

            # 5. Query relations: itemRelations
            if has_item_relations:
                sql_relations = f"""
                    SELECT ir.itemID, ir.object
                    FROM itemRelations ir
                    WHERE ir.itemID IN ({placeholders})
                      {del_clause_relations}
                    ORDER BY ir.itemID, ir.rowid
                """
                relations_by_item: dict[int, list[str]] = defaultdict(list)
                seen_relations: dict[int, set[str]] = defaultdict(set)
                for r in conn.execute(sql_relations, batch_ids).fetchall():
                    item_id = r["itemID"]
                    raw_uri = str(r["object"] or "").strip()
                    if "/items/" not in raw_uri:
                        continue
                    dest_key = (
                        raw_uri.split("/items/")[-1]
                        .strip()
                        .split("/")[0]
                        .split("?")[0]
                        .split("#")[0]
                    )
                    if (
                        dest_key
                        and dest_key not in del_keys
                        and dest_key not in seen_relations[item_id]
                    ):
                        seen_relations[item_id].add(dest_key)
                        relations_by_item[item_id].append(dest_key)

                for item in batch_items:
                    item.relations = list(relations_by_item.get(item.item_id, []))

            items.extend(batch_items)

        return items

    def _build_item(
        self, conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
    ) -> ZoteroItem:
        """Construct and populate a ZoteroItem from a query row."""
        return self._build_items_batched(conn, [row])[0]

    def _fetch_relations(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Return target item keys for relations originating from item_id.

        Args:
            conn: Open SQLite connection to reuse.
            item_id: Numeric Zotero item ID.

        Returns:
            List of target item key strings extracted from relation URIs.
        """
        if not self._table_exists(conn, "itemRelations"):
            return []

        has_deleted_items = self._table_exists(conn, "deletedItems")
        del_clause = "AND ir.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
        sql = f"""
            SELECT ir.object
            FROM itemRelations ir
            WHERE ir.itemID = ? {del_clause}
        """
        rows = conn.execute(sql, (item_id,)).fetchall()

        del_keys: set[str] = set()
        if has_deleted_items and self._table_exists(conn, "items"):
            del_rows = conn.execute(
                "SELECT key FROM items WHERE itemID IN (SELECT itemID FROM deletedItems)"
            ).fetchall()
            del_keys = {r["key"] for r in del_rows if r["key"]}

        relations: list[str] = []
        seen: set[str] = set()
        for r in rows:
            raw_uri = str(r["object"] or "").strip()
            if "/items/" not in raw_uri:
                continue
            dest_key = raw_uri.split("/items/")[-1].strip().split("/")[0].split("?")[0].split("#")[0]
            if dest_key and dest_key not in del_keys and dest_key not in seen:
                seen.add(dest_key)
                relations.append(dest_key)

        return relations

    def get_item_relations(self, item_key_or_id: str | int) -> list[str]:
        """Return target item keys related to the specified item."""
        with _open_db(self._sqlite_path) as conn:
            has_deleted_items = self._table_exists(conn, "deletedItems")
            item_id: int | None = None
            if isinstance(item_key_or_id, int):
                if has_deleted_items:
                    del_row = conn.execute(
                        "SELECT 1 FROM deletedItems WHERE itemID = ?",
                        (item_key_or_id,),
                    ).fetchone()
                    if del_row:
                        return []
                item_id = item_key_or_id
            else:
                del_clause = "AND itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
                row = conn.execute(
                    f"SELECT itemID FROM items WHERE key = ? {del_clause}",
                    (item_key_or_id,),
                ).fetchone()
                if not row:
                    return []
                item_id = row["itemID"]

            return self._fetch_relations(conn, item_id)

    def get_all_item_relations(self) -> list[tuple[str, str]]:
        """Return all explicit item relations as (source_key, target_key) pairs."""
        with _open_db(self._sqlite_path) as conn:
            if not self._table_exists(conn, "itemRelations"):
                return []

            has_deleted_items = self._table_exists(conn, "deletedItems")
            del_clause = "WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
            sql = f"""
                SELECT i.key AS source_key, ir.object AS target_uri
                FROM itemRelations ir
                JOIN items i ON ir.itemID = i.itemID
                {del_clause}
            """
            rows = conn.execute(sql).fetchall()

            del_keys: set[str] = set()
            if has_deleted_items and self._table_exists(conn, "items"):
                del_rows = conn.execute(
                    "SELECT key FROM items WHERE itemID IN (SELECT itemID FROM deletedItems)"
                ).fetchall()
                del_keys = {r["key"] for r in del_rows if r["key"]}

            relations: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for r in rows:
                src = str(r["source_key"] or "").strip()
                target_uri = str(r["target_uri"] or "").strip()
                if not src or "/items/" not in target_uri:
                    continue
                target_key = target_uri.split("/items/")[-1].strip().split("/")[0].split("?")[0].split("#")[0]
                if target_key and target_key not in del_keys and target_key != src:
                    pair = (src, target_key)
                    if pair not in seen:
                        seen.add(pair)
                        relations.append(pair)

            return relations

    def get_item_tags(
        self, item_key_or_id: str | int, tag_type: int | None = None
    ) -> list[str]:
        """Return tag names for an item, optionally filtered by tag type (0=user, 1=automatic)."""
        with _open_db(self._sqlite_path) as conn:
            has_item_tags = self._table_exists(conn, "itemTags")
            if not has_item_tags:
                return []

            item_id: int | None = None
            has_deleted_items = self._table_exists(conn, "deletedItems")
            if isinstance(item_key_or_id, int):
                if has_deleted_items:
                    del_row = conn.execute(
                        "SELECT 1 FROM deletedItems WHERE itemID = ?",
                        (item_key_or_id,),
                    ).fetchone()
                    if del_row:
                        return []
                item_id = item_key_or_id
            else:
                del_clause = "AND itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted_items else ""
                row = conn.execute(
                    f"SELECT itemID FROM items WHERE key = ? {del_clause}",
                    (item_key_or_id,),
                ).fetchone()
                if not row:
                    return []
                item_id = row["itemID"]

            params: list[Any] = [item_id]
            type_clause = ""
            if tag_type is not None:
                cols = {c[1] for c in conn.execute("PRAGMA table_info(itemTags)").fetchall()}
                if "type" in cols:
                    type_clause = " AND it.type = ?"
                    params.append(tag_type)

            sql = f"""
                SELECT DISTINCT t.name
                FROM itemTags it
                JOIN tags t ON it.tagID = t.tagID
                WHERE it.itemID = ?{type_clause}
                ORDER BY t.name
            """
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                has_attachments_table = self._table_exists(conn, "itemAttachments")
                if has_attachments_table:
                    p_row = conn.execute(
                        "SELECT parentItemID FROM itemAttachments WHERE itemID = ?",
                        (item_id,),
                    ).fetchone()
                    if p_row and p_row["parentItemID"]:
                        params[0] = p_row["parentItemID"]
                        rows = conn.execute(sql, params).fetchall()

            return [r["name"] for r in rows if r["name"]]

    def get_item_keys_in_collection(
        self, collection_name: str, recursive: bool = True
    ) -> list[str]:
        """Return the unique item keys for all items belonging to a collection name or key.

        Args:
            collection_name: Name or key of the collection.
            recursive: If True and parentCollectionID exists, includes items in all subcollections.

        Returns:
            List of unique item keys.
        """
        with _open_db(self._sqlite_path) as conn:
            has_deleted = self._table_exists(conn, "deletedItems")
            del_clause = "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)" if has_deleted else ""

            col_columns = {c[1] for c in conn.execute("PRAGMA table_info(collections)").fetchall()}
            has_parent_col = "parentCollectionID" in col_columns

            if recursive and has_parent_col:
                sql = f"""
                    WITH RECURSIVE subcols(collectionID) AS (
                        SELECT collectionID
                        FROM collections
                        WHERE collectionName = ? OR key = ?
                        UNION
                        SELECT c.collectionID
                        FROM collections c
                        JOIN subcols s ON c.parentCollectionID = s.collectionID
                    )
                    SELECT DISTINCT i.key
                    FROM items i
                    JOIN collectionItems ci ON i.itemID = ci.itemID
                    JOIN subcols s ON ci.collectionID = s.collectionID
                    WHERE 1=1 {del_clause}
                """
            else:
                sql = f"""
                    SELECT DISTINCT i.key
                    FROM items i
                    JOIN collectionItems ci ON i.itemID = ci.itemID
                    JOIN collections col ON ci.collectionID = col.collectionID
                    WHERE (col.collectionName = ? OR col.key = ?)
                      {del_clause}
                """
            rows = conn.execute(sql, (collection_name, collection_name)).fetchall()
            return [row["key"] for row in rows]
