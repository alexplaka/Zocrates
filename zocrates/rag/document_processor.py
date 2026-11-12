"""
Document processor: converts PDF/HTML attachments to Markdown and caches them.

Supports pluggable converter strategies via `BaseDocumentConverter`.
Default converters include:
  - DirectReadConverter: Fast UTF-8 reader for .md and .txt files.
  - MarkItDownConverter: Uses MarkItDown for document conversion with page-by-page
    PDF image extraction via system utility `pdfimages`.

The cache directory lives in:
  <ZoteroDataDirectory>/zocrates-data/corpus/
Each item gets an item-centric bundle directory:
  corpus/<item_key>/document.md   -- converted Markdown
  corpus/<item_key>/images/       -- extracted images
  corpus/conversion_meta.db       -- SQLite metadata (authoritative)
"""

import hashlib
import html as html_module
import json
import logging
import re
import shutil
import sqlite3
import threading
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zocrates.rag.document_converters import (
    BaseDocumentConverter,
    DatalabConverter,
    DirectReadConverter,
    DoclingLocalConverter,
    MarkItDownConverter,
    wrap_standalone_markdown_tables,
)
from zocrates.rag.bundle_catalog import (
    build_frontmatter,
    generate_item_index_md,
    generate_root_index_md,
    normalize_to_storage_relative,
    regenerate_item_relations_md,
    strip_existing_frontmatter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metadata DB helpers
# ---------------------------------------------------------------------------

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversion_meta (
    item_key            TEXT PRIMARY KEY,
    title               TEXT,
    converter_id        TEXT NOT NULL,
    converter_version   TEXT NOT NULL,
    source_path         TEXT NOT NULL,
    content_type        TEXT NOT NULL,
    parent_item_type    TEXT,
    converted_at        TEXT NOT NULL,
    indexed_at          TEXT,
    source_hash         TEXT NOT NULL,   -- SHA-256 of the source file
    char_count          INTEGER NOT NULL,
    parent_item_key     TEXT,
    parse_quality_score REAL,
    checkpoint_id       TEXT,
    abstract            TEXT,
    abstract_source     TEXT,
    keywords            TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS conversion_failures (
    item_key       TEXT NOT NULL,
    converter_id   TEXT NOT NULL,
    error_message  TEXT NOT NULL,
    source_hash    TEXT NOT NULL,
    failed_at      TEXT NOT NULL,
    PRIMARY KEY (item_key, converter_id)
);

CREATE TABLE IF NOT EXISTS item_relations (
    source_key      TEXT NOT NULL,
    target_key      TEXT NOT NULL,
    score           REAL NOT NULL,
    relation_type   TEXT NOT NULL,
    match_evidence  TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (source_key, target_key, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_relations_source_score ON item_relations (source_key, score DESC);
CREATE INDEX IF NOT EXISTS idx_relations_target ON item_relations (target_key);
"""


_META_INIT_LOCK = threading.Lock()
_INITIALIZED_META_DBS: set[Path] = set()

# Lock protecting concurrent note bundle materialization (atomic directory swap).
_note_bundle_lock = threading.Lock()
# Lock protecting concurrent metadata bundle materialization (atomic directory swap).
_metadata_bundle_lock = threading.Lock()


def _open_meta_db(cache_dir: Path) -> sqlite3.Connection:
    """Open (or create) the conversion metadata SQLite database.

    Creates the ``conversion_meta`` table if it does not already exist.
    Enables WAL mode for concurrent readers.

    Args:
        cache_dir: Directory containing the ``conversion_meta.db`` file.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory`` set to
        ``sqlite3.Row``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cache_dir / "conversion_meta.db"
    conn = sqlite3.connect(str(meta_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")

    if cache_dir not in _INITIALIZED_META_DBS:
        with _META_INIT_LOCK:
            if cache_dir not in _INITIALIZED_META_DBS:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.executescript(_META_SCHEMA)
                # Migrate columns on pre-existing databases if needed
                existing_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(conversion_meta)").fetchall()
                }
                if "title" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN title TEXT;")
                    except sqlite3.OperationalError:
                        pass
                if "parse_quality_score" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN parse_quality_score REAL;")
                    except sqlite3.OperationalError:
                        pass
                if "checkpoint_id" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN checkpoint_id TEXT;")
                    except sqlite3.OperationalError:
                        pass
                if "abstract" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN abstract TEXT;")
                    except sqlite3.OperationalError:
                        pass
                if "abstract_source" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN abstract_source TEXT;")
                    except sqlite3.OperationalError:
                        pass
                if "updated_at" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN updated_at TEXT;")
                    except sqlite3.OperationalError:
                        pass
                if "keywords" not in existing_cols:
                    try:
                        conn.execute("ALTER TABLE conversion_meta ADD COLUMN keywords TEXT;")
                    except sqlite3.OperationalError:
                        pass
                try:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS item_relations (
                            source_key      TEXT NOT NULL,
                            target_key      TEXT NOT NULL,
                            score           REAL NOT NULL,
                            relation_type   TEXT NOT NULL,
                            match_evidence  TEXT,
                            updated_at      TEXT NOT NULL,
                            PRIMARY KEY (source_key, target_key, relation_type)
                        );
                        """
                    )
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_source_score ON item_relations (source_key, score DESC);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_target ON item_relations (target_key);")
                except sqlite3.OperationalError:
                    pass
                conn.commit()

                # Migrate storage paths to relative and sanitize legacy holdover bundles
                meta_path = cache_dir / "conversion_meta.db"
                version_row = conn.execute("PRAGMA user_version;").fetchone()
                current_version = version_row[0] if version_row else 0
                if current_version < 1:
                    _migrate_storage_paths_to_relative(meta_path, conn=conn)
                    _sanitize_cached_bundle_image_links(cache_dir)

                _INITIALIZED_META_DBS.add(cache_dir)

    return conn


def _relativize_staging_markdown_links(markdown: str, tmp_dir: Path) -> str:
    """Rewrite any temporary staging directory paths/URIs to bundle-relative images/ links."""
    tmp_uri = tmp_dir.as_uri()
    tmp_str = str(tmp_dir)
    tmp_posix = tmp_dir.as_posix()

    for prefix in (tmp_uri, tmp_str, tmp_posix):
        markdown = markdown.replace(f"{prefix}/images/", "images/")
        markdown = markdown.replace(f"{prefix}/", "")

    def _sanitize_img(m: re.Match) -> str:
        """Sanitize file:// URL image paths to relative images/<name> format.

        Args:
            m: Regex match object containing the raw image filename.

        Returns:
            Relative markdown image path.
        """
        return f"images/{urllib.parse.unquote(m.group(1))}"

    markdown = re.sub(
        r"file://[^\s\)\"']+?[/\\]images[/\\]([^\s\)\"']+)",
        _sanitize_img,
        markdown,
    )
    return markdown


def _migrate_storage_paths_to_relative(
    db_path: Path, conn: sqlite3.Connection | None = None
) -> None:
    """Migrate legacy absolute source_path entries to storage-relative paths.

    Idempotent and atomic. Upgrades PRAGMA user_version from 0 to 1.
    Creates a pre-migration snapshot backup if not already present.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        close_conn = True

    try:
        conn.execute("PRAGMA busy_timeout = 5000;")
        cursor = conn.execute("PRAGMA user_version;")
        version = cursor.fetchone()[0]
        if version >= 1:
            return

        # Pre-migration snapshot backup
        bak_path = db_path.with_name(f"{db_path.name}.bak-pre-relativization")
        if not bak_path.exists() and db_path.exists():
            try:
                bck = sqlite3.connect(str(bak_path))
                conn.backup(bck)
                bck.close()
            except Exception:
                try:
                    shutil.copy2(db_path, bak_path)
                except Exception as exc:
                    logger.warning("Could not create pre-migration backup %s: %s", bak_path, exc)

        cursor = conn.execute(
            "SELECT item_key, source_path FROM conversion_meta "
            "WHERE source_path NOT LIKE 'storage/%' AND source_path NOT LIKE 'zotero://%';"
        )
        rows = cursor.fetchall()
        updates = []
        pattern = re.compile(
            r"(?:^|[/\\])storage[/\\](?P<key>[A-Za-z0-9_-]+)(?:[/\\](?P<filename>.+))?$"
        )

        for row in rows:
            key = row["item_key"]
            src = row["source_path"] or ""
            unquoted_src = urllib.parse.unquote(src)
            m = pattern.search(unquoted_src) or pattern.search(src)
            # Key-equality guard: only rewrite when captured key matches the row's item_key
            if m and m.group("key") == key:
                match_key = m.group("key")
                match_file = (m.group("filename") or "").replace("\\", "/").rstrip("/")
                rel_path = (
                    f"storage/{match_key}/{match_file}"
                    if match_file
                    else f"storage/{match_key}"
                )
                updates.append((rel_path, key))

        conn.execute("BEGIN IMMEDIATE;")
        if updates:
            conn.executemany(
                "UPDATE conversion_meta SET source_path = ? WHERE item_key = ?;",
                updates,
            )
        conn.execute("PRAGMA user_version = 1;")
        conn.commit()
    finally:
        if close_conn:
            conn.close()


def sanitize_image_links_in_markdown(text: str) -> str:
    """Sanitize legacy file:///.../images/ absolute links in markdown text.

    Ensures strict line-count invariance to preserve ToC and chunk line offsets.

    Args:
        text: Raw markdown text.

    Returns:
        Markdown text with file:///.../images/<name> rewritten to images/<name>.
    """
    if "file://" not in text or "images/" not in text:
        return text
    pattern = re.compile(r"file://[^\s\)\"']+?[/\\]images[/\\]([^\s\)\"']+)")
    return pattern.sub(lambda m: f"images/{urllib.parse.unquote(m.group(1))}", text)


def _sanitize_cached_bundle_image_links(cache_dir: Path) -> int:
    """Sanitize legacy file:///.../images/ absolute links in cached document.md files.

    Ensures strict line-count invariance to preserve ToC and chunk line offsets.

    Args:
        cache_dir: Root corpus directory.

    Returns:
        Count of sanitized document bundles.
    """
    if not cache_dir.exists():
        return 0

    sanitized_count = 0
    for item_dir in cache_dir.iterdir():
        if not item_dir.is_dir() or item_dir.name.startswith("."):
            continue
        doc_file = item_dir / "document.md"
        if not doc_file.exists():
            continue

        try:
            content = doc_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("Could not read %s for image sanitization: %s", doc_file, exc)
            continue

        new_content = sanitize_image_links_in_markdown(content)
        if new_content == content:
            continue

        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        if len(old_lines) != len(new_lines):
            logger.warning(
                "Line count mismatch when sanitizing %s (%d vs %d lines); aborting edit.",
                doc_file,
                len(old_lines),
                len(new_lines),
            )
            continue

        # Atomic intra-line replacement
        tmp_file = item_dir / "document.md.tmp"
        try:
            tmp_file.write_text(new_content, encoding="utf-8")
            tmp_file.replace(doc_file)
            sanitized_count += 1
        except Exception as exc:
            logger.warning("Failed writing sanitized %s: %s", doc_file, exc)
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)

    return sanitized_count


def _file_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file.

    Reads in 64 KiB chunks to keep memory usage constant for large PDFs.

    Args:
        path: Absolute path to the file to hash.

    Returns:
        Lowercase hex string of the SHA-256 digest.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class UnsupportedConversionError(Exception):
    """Raised when a converter does not support the given content type."""


CONVERTER_REGISTRY: dict[str, type[BaseDocumentConverter]] = {
    "direct_read": DirectReadConverter,
    "markitdown": MarkItDownConverter,
    "docling_local": DoclingLocalConverter,
    "datalab": DatalabConverter,
}


class DocumentProcessor:
    """
    Converts attachments (PDF, HTML, etc.) to Markdown with local caching and image extraction.
    Manages cache persistence and delegates conversion to registered BaseDocumentConverters.
    """

    CONVERTER_REGISTRY = CONVERTER_REGISTRY

    def __init__(
            self,
            cache_dir: Path,
            converters: list[BaseDocumentConverter] | None = None,
            document_converter_id: str | None = None,
            datalab_non_pdf_fallback: str = "markitdown",
    ) -> None:
        """Initialize the DocumentProcessor.

        Args:
            cache_dir: Directory used to store converted Markdown files and the
                ``conversion_meta.db`` metadata database. Created if it does
                not exist.
            converters: Optional list of document converters. If None, defaults to
                [DirectReadConverter(), MarkItDownConverter(), DoclingLocalConverter(), DatalabConverter()].
            document_converter_id: Global active converter ID from config.
                When set, ``get_or_convert`` prefers this converter on cache miss
                before falling back to the priority chain. If ``None``, the
                priority chain is used directly.
            datalab_non_pdf_fallback: Fallback converter ID when active converter
                is 'datalab' and content_type is not PDF ('markitdown' or 'docling_local').
        """
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._document_converter_id = document_converter_id
        self._datalab_non_pdf_fallback = datalab_non_pdf_fallback
        if converters is None:
            self._converters: list[BaseDocumentConverter] = [
                DirectReadConverter(),
                MarkItDownConverter(),
                DoclingLocalConverter(),
                DatalabConverter(),
            ]
        else:
            self._converters = list(converters)

    @property
    def datalab_non_pdf_fallback(self) -> str:
        """Return the fallback converter ID for non-PDF items when Datalab is active."""
        return self._datalab_non_pdf_fallback

    @datalab_non_pdf_fallback.setter
    def datalab_non_pdf_fallback(self, value: str) -> None:
        """Set the fallback converter ID for non-PDF items when Datalab is active.

        Args:
            value: Converter strategy ID string (e.g. 'docling_local', 'markitdown').
        """
        self._datalab_non_pdf_fallback = value

    @property
    def cache_dir(self) -> Path:
        """Return the root markdown cache directory Path."""
        return self._cache_dir

    @classmethod
    def _get_default_cache_dir(cls) -> Path | None:
        """Return the default corpus directory if discoverable."""
        try:
            from zocrates.config import get_corpus_dir, resolve_zotero_data_dir

            zotero_dir = resolve_zotero_data_dir()
            if zotero_dir:
                return get_corpus_dir(zotero_dir)
        except Exception:
            pass
        return None

    def register_converter(
            self, converter: BaseDocumentConverter, prepend: bool = True
    ) -> None:
        """Register a new document converter strategy.

        Args:
            converter: Instance of BaseDocumentConverter to register.
            prepend: If True (default), inserts the converter at the highest priority.
        """
        if prepend:
            self._converters.insert(0, converter)
        else:
            self._converters.append(converter)

    def regenerate_root_index(self) -> None:
        """Regenerate the root-level index.md catalog from conversion_meta.db."""
        generate_root_index_md(self._cache_dir)

    def get_or_convert(
            self,
            item_key: str,
            source_path: Path,
            content_type: str,
            parent_item_type: str | None = None,
            title: str | None = None,
            collections: list[Any] | None = None,
            parent_item_key: str | None = None,
            authors: list[str] | None = None,
            year: str | int | None = None,
            publication: str | None = None,
            doi: str | None = None,
            url: str | None = None,
            abstract: str | None = None,
            abstract_source: str | None = None,
            update_root_index: bool = True,
    ) -> str | None:
        """Return the Markdown for an attachment, converting if not already cached
        or if the source file has changed since the last conversion.

        Args:
            item_key: Zotero item key.
            source_path: Path to the source file on disk.
            content_type: MIME type of the source file.
            parent_item_type: Zotero parent item type (e.g. 'journalArticle').
            title: Parent item title.
            collections: Parent item collections list.
            parent_item_key: Parent Zotero item key string.
            authors: Optional author names list.
            year: Optional publication year.
            publication: Optional publication venue string.
            doi: Optional DOI string.
            url: Optional source URL string.
            abstract: Optional abstract or summary text.
            abstract_source: Optional source of abstract ('zotero', 'generated', 'extractive').
            update_root_index: If True (default), updates the root index.md catalog.

        Returns:
            The converted Markdown text with YAML frontmatter, or None on error.
        """
        if not source_path.exists():
            logger.warning("Attachment not found on disk: %s", source_path)
            return None

        current_hash = _file_sha256(source_path)
        cached_md = self._load_from_cache(item_key, current_hash)
        if cached_md is not None:
            return cached_md

        # Convert using active converter or priority chain fallback
        converter = self._resolve_converter(content_type, source_path)
        if converter is None:
            logger.warning("No suitable converter found for %s", source_path)
            return None

        markdown = self._run_converter(converter, item_key, source_path, content_type)
        if markdown is None:
            return None

        full_md = self._save_to_cache(
            item_key, source_path, content_type, current_hash, markdown,
            converter.converter_id, converter.converter_version,
            parent_item_type=parent_item_type, title=title, collections=collections,
            parent_item_key=parent_item_key,
            authors=authors, year=year, publication=publication, doi=doi, url=url,
            abstract=abstract, abstract_source=abstract_source,
            update_root_index=update_root_index,
        )
        return full_md

    def convert_with(
            self,
            item_key: str,
            converter_id: str,
            source_path: Path,
            content_type: str,
            parent_item_type: str | None = None,
            title: str | None = None,
            collections: list[Any] | None = None,
            parent_item_key: str | None = None,
            authors: list[str] | None = None,
            year: str | int | None = None,
            publication: str | None = None,
            doi: str | None = None,
            url: str | None = None,
            abstract: str | None = None,
            abstract_source: str | None = None,
            update_root_index: bool = True,
    ) -> str:
        """Convert a document with a specific converter engine.

        This is the explicit user-action path. No fallback -- if the
        specified converter doesn't support the content type, an error is
        raised. Any existing conversion for this item is destroyed and
        replaced.

        The caller is responsible for re-indexing the vector store after
        this method returns.

        Args:
            item_key: Zotero item key.
            converter_id: ID of the converter to use.
            source_path: Path to the source file on disk.
            content_type: MIME type of the source file.
            parent_item_type: Zotero parent item type.
            title: Parent item title.
            collections: Parent item collections.
            parent_item_key: Parent Zotero item key string.
            authors: Optional author names list.
            year: Optional publication year.
            publication: Optional publication venue string.
            doi: Optional DOI string.
            url: Optional source URL string.
            abstract: Optional item abstract.
            abstract_source: Optional source of item abstract.
            update_root_index: If True (default), updates the root index.md.

        Returns:
            The converted Markdown text (with YAML frontmatter).

        Raises:
            UnsupportedConversionError: If the converter doesn't support
                the content type.
            KeyError: If no converter with ``converter_id`` is registered.
        """
        converter = self._get_converter_by_id(converter_id)
        if not converter.supports(content_type, source_path):
            # If requested converter is datalab (which is PDF-only), route non-PDFs to configured fallback
            fallback_conv = None
            if converter_id == "datalab":
                fallback_target = self._datalab_non_pdf_fallback or "markitdown"
                try:
                    target_c = self._get_converter_by_id(fallback_target)
                    if target_c.supports(content_type, source_path):
                        fallback_conv = target_c
                        logger.info(
                            "Converter '%s' does not support '%s'; routing to configured fallback '%s' for %s",
                            converter_id, content_type, target_c.converter_id, source_path.name,
                        )
                except KeyError:
                    pass

                if fallback_conv is None:
                    fallback_conv = self._resolve_converter(content_type, source_path)

            if fallback_conv is not None and fallback_conv.supports(content_type, source_path):
                converter = fallback_conv
                converter_id = fallback_conv.converter_id
            else:
                raise UnsupportedConversionError(
                    f"Converter '{converter_id}' does not support "
                    f"content type '{content_type}'"
                )

        # Convert into a temporary directory first so the existing bundle
        # is preserved if conversion fails.
        tmp_dir = self._cache_dir / f".tmp_{item_key}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        current_hash = _file_sha256(source_path)

        try:
            markdown = converter.convert(
                item_key, source_path, content_type, tmp_dir
            )
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.record_failure(item_key, converter_id, str(exc), current_hash)
            raise RuntimeError(
                f"Converter '{converter_id}' failed for {source_path}: {exc}"
            ) from exc

        if markdown is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            err_msg = f"Converter '{converter_id}' returned empty content for {source_path}"
            self.record_failure(item_key, converter_id, err_msg, current_hash)
            raise RuntimeError(err_msg)

        # Conversion succeeded -- destroy old bundle and swap in the new one
        item_dir = self._cache_dir / item_key
        if item_dir.exists():
            shutil.rmtree(item_dir)
        tmp_dir.rename(item_dir)

        # Replace any temporary directory references in the converted markdown
        markdown = _relativize_staging_markdown_links(markdown, tmp_dir)

        self.clear_failure(item_key, converter_id)
        if converter.converter_id != converter_id:
            self.clear_failure(item_key, converter.converter_id)

        self._save_to_cache(
            item_key, source_path, content_type, current_hash, markdown,
            converter.converter_id, converter.converter_version,
            parent_item_type=parent_item_type, title=title, collections=collections,
            parent_item_key=parent_item_key,
            authors=authors, year=year, publication=publication, doi=doi, url=url,
            abstract=abstract, abstract_source=abstract_source,
            update_root_index=update_root_index,
        )
        return self.read_cached(item_key) or markdown

    def delete_cached(self, item_key: str, update_root_index: bool = True) -> bool:
        """Delete an item bundle from disk and conversion_meta DB.

        Args:
            item_key: Zotero item key to remove.
            update_root_index: If True (default), updates the root index.md catalog.

        Returns:
            True if an item bundle or DB row was deleted, False if item was not cached.
        """
        deleted = False
        item_dir = self._cache_dir / item_key
        if item_dir.exists():
            shutil.rmtree(item_dir, ignore_errors=True)
            deleted = True

        affected_neighbors: list[str] = []
        with _open_meta_db(self._cache_dir) as conn:
            try:
                cur = conn.execute(
                    """
                    SELECT DISTINCT target_key FROM item_relations WHERE source_key = ?
                    UNION
                    SELECT DISTINCT source_key FROM item_relations WHERE target_key = ?
                    """,
                    (item_key, item_key),
                )
                affected_neighbors = [row[0] for row in cur.fetchall() if row[0] != item_key]
                conn.execute(
                    "DELETE FROM item_relations WHERE source_key = ? OR target_key = ?",
                    (item_key, item_key),
                )
            except sqlite3.OperationalError:
                pass

            cur = conn.execute(
                "DELETE FROM conversion_meta WHERE item_key = ?", (item_key,)
            )
            if cur.rowcount > 0:
                deleted = True
            conn.execute(
                "DELETE FROM conversion_failures WHERE item_key = ?", (item_key,)
            )
            conn.commit()

        # Regenerate item_relations.md for all affected neighbors
        for neighbor_key in affected_neighbors:
            try:
                regenerate_item_relations_md(neighbor_key, self._cache_dir)
            except Exception as exc:
                logger.warning(
                    "Failed to regenerate item_relations.md for neighbor %s during deletion of %s: %s",
                    neighbor_key, item_key, exc,
                )

        if deleted and update_root_index:
            generate_root_index_md(self._cache_dir)

        return deleted

    def has_child_attachments(self, parent_item_key: str) -> bool:
        """Return True if any attachment bundle exists in conversion_meta for parent_item_key."""
        if not parent_item_key:
            return False
        with _open_meta_db(self._cache_dir) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM conversion_meta
                WHERE parent_item_key = ? AND content_type NOT IN ('text/note', 'text/zotero-metadata')
                LIMIT 1
                """,
                (parent_item_key,),
            ).fetchone()
            return row is not None

    def update_indexed_at(self, item_key: str, timestamp: str | None = None) -> None:
        """Update the indexed_at timestamp for item_key in conversion_meta DB.

        Args:
            item_key: Zotero item key.
            timestamp: Optional ISO-8601 timestamp string. Defaults to current UTC time.
        """
        ts = timestamp or datetime.now(UTC).isoformat()
        with _open_meta_db(self._cache_dir) as conn:
            conn.execute(
                "UPDATE conversion_meta SET indexed_at = ? WHERE item_key = ?",
                (ts, item_key),
            )
            conn.commit()

    def read_cached(self, item_key: str) -> str | None:
        """Read cached Markdown without triggering conversion."""
        md_path = self._cache_dir / item_key / "document.md"
        if md_path.exists():
            return md_path.read_text(encoding="utf-8")
        return None

    def is_cached(self, item_key: str) -> bool:
        """Return True if a converted Markdown file exists for item_key.

        Does not validate freshness against the source file hash.

        Args:
            item_key: Zotero item key.

        Returns:
            ``True`` if ``<item_key>/document.md`` exists, ``False`` otherwise.
        """
        md_path = self._cache_dir / item_key / "document.md"
        return md_path.exists()

    def get_metadata(self, item_key: str) -> dict | None:
        """Fetch metadata dictionary for an item key from conversion_meta.db."""
        with _open_meta_db(self._cache_dir) as conn:
            row = conn.execute(
                """
                SELECT item_key, title, converter_id, converter_version, source_path, content_type,
                       parent_item_type, converted_at, indexed_at, source_hash, char_count,
                       parent_item_key, parse_quality_score, checkpoint_id, abstract, abstract_source
                FROM conversion_meta WHERE item_key = ?
                """,
                (item_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "item_key": row["item_key"],
            "title": row["title"],
            "converter_id": row["converter_id"],
            "converter_version": row["converter_version"],
            "source_path": row["source_path"],
            "content_type": row["content_type"],
            "parent_item_type": row["parent_item_type"],
            "converted_at": row["converted_at"],
            "indexed_at": row["indexed_at"],
            "source_hash": row["source_hash"],
            "char_count": row["char_count"],
            "parent_item_key": row["parent_item_key"],
            "parse_quality_score": row["parse_quality_score"],
            "checkpoint_id": row["checkpoint_id"],
            "abstract": row["abstract"] if "abstract" in row.keys() else None,
            "abstract_source": row["abstract_source"] if "abstract_source" in row.keys() else None,
        }

    def record_failure(
        self, item_key: str, converter_id: str, error_message: str, source_hash: str
    ) -> None:
        """Record or update a failed conversion attempt for an item.

        Args:
            item_key: Zotero item key.
            converter_id: Converter engine ID that failed.
            error_message: Error description or exception string.
            source_hash: SHA-256 hex digest of the source file at failure time.
        """
        failed_at = datetime.now(UTC).isoformat()
        with _open_meta_db(self._cache_dir) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conversion_failures
                    (item_key, converter_id, error_message, source_hash, failed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_key, converter_id, error_message, source_hash, failed_at),
            )
            conn.commit()

    def clear_failure(self, item_key: str, converter_id: str | None = None) -> None:
        """Clear failure record(s) for an item and optionally a specific converter.

        Args:
            item_key: Zotero item key.
            converter_id: Specific converter ID to clear, or None to clear all.
        """
        with _open_meta_db(self._cache_dir) as conn:
            if converter_id is not None:
                conn.execute(
                    "DELETE FROM conversion_failures WHERE item_key = ? AND converter_id = ?",
                    (item_key, converter_id),
                )
            else:
                conn.execute(
                    "DELETE FROM conversion_failures WHERE item_key = ?",
                    (item_key,),
                )
            conn.commit()

    def get_failures(self, item_key: str) -> dict[str, dict[str, Any]]:
        """Retrieve recorded conversion failures for an item key.

        Args:
            item_key: Zotero item key.

        Returns:
            Dict mapping converter_id -> failure details dict.
        """
        with _open_meta_db(self._cache_dir) as conn:
            rows = conn.execute(
                """
                SELECT converter_id, error_message, source_hash, failed_at
                FROM conversion_failures WHERE item_key = ?
                """,
                (item_key,),
            ).fetchall()
        return {
            row["converter_id"]: {
                "error_message": row["error_message"],
                "source_hash": row["source_hash"],
                "failed_at": row["failed_at"],
            }
            for row in rows
        }

    def ensure_parent_item_key(self, item_key: str, parent_item_key: str) -> None:
        """Ensure an item in conversion_meta has parent_item_key populated."""
        if not item_key or not parent_item_key:
            return
        with _open_meta_db(self._cache_dir) as conn:
            conn.execute(
                """
                UPDATE conversion_meta
                SET parent_item_key = ?
                WHERE item_key = ? AND (parent_item_key IS NULL OR parent_item_key = '')
                """,
                (parent_item_key, item_key),
            )
            conn.commit()

    def update_abstract(
        self,
        item_key: str,
        abstract: str | None,
        abstract_source: str | None = "zotero",
    ) -> None:
        """Update abstract and abstract_source in conversion_meta for an item."""
        if not item_key:
            return
        with _open_meta_db(self._cache_dir) as conn:
            conn.execute(
                """
                UPDATE conversion_meta
                SET abstract = ?, abstract_source = ?
                WHERE item_key = ?
                """,
                (abstract, abstract_source, item_key),
            )
            conn.commit()

    def get_all_metadata(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all cached documents in a single fast query.

        Returns:
            Dictionary mapping ``item_key`` to metadata dictionary.
        """
        with _open_meta_db(self._cache_dir) as conn:
            rows = conn.execute("SELECT * FROM conversion_meta").fetchall()
            return {
                row["item_key"]: {
                    "item_key": row["item_key"],
                    "title": row["title"] if "title" in row.keys() else None,
                    "converter_id": row["converter_id"],
                    "converter_version": row["converter_version"],
                    "source_path": row["source_path"],
                    "content_type": row["content_type"],
                    "parent_item_type": row["parent_item_type"],
                    "converted_at": row["converted_at"],
                    "indexed_at": row["indexed_at"],
                    "source_hash": row["source_hash"],
                    "char_count": row["char_count"],
                    "parent_item_key": row["parent_item_key"],
                    "parse_quality_score": row["parse_quality_score"],
                    "checkpoint_id": row["checkpoint_id"],
                    "abstract": row["abstract"] if "abstract" in row.keys() else None,
                    "abstract_source": row["abstract_source"] if "abstract_source" in row.keys() else None,
                }
                for row in rows
            }

    def list_cached_keys(self) -> list[str]:
        """Return the item keys of all documents currently in the Markdown cache.

        Returns:
            List of item key strings (subdirectories containing ``document.md``).
        """
        return [
            p.parent.name
            for p in self._cache_dir.glob("*/document.md")
        ]

    def get_all_meta_keys(self) -> set[str]:
        """Return the set of all unique item keys tracked in conversion_meta."""
        try:
            with _open_meta_db(self._cache_dir) as conn:
                rows = conn.execute("SELECT DISTINCT item_key FROM conversion_meta").fetchall()
                return {row["item_key"] for row in rows if row["item_key"]}
        except Exception as exc:
            logger.error("Failed to query item keys from conversion_meta: %s", exc)
            return set()

    # ---- private -----------------------------------------------------------

    def _get_converter_by_id(self, converter_id: str) -> BaseDocumentConverter:
        """Look up a registered converter by its ID.

        Raises:
            KeyError: If no converter with the given ID is registered.
        """
        for converter in self._converters:
            if converter.converter_id == converter_id:
                return converter
            if converter_id == "docling" and converter.converter_id == "docling_local":
                return converter
        registered = [c.converter_id for c in self._converters]
        raise KeyError(
            f"No converter registered with id '{converter_id}'. "
            f"Available: {registered}"
        )

    def _resolve_converter(
            self, content_type: str, source_path: Path
    ) -> BaseDocumentConverter | None:
        """Select the converter for auto-dispatch (get_or_convert).

        Tries the active converter first. If it doesn't support the content
        type, falls back to the priority chain with a log notification.
        """
        # Try active converter first
        if self._document_converter_id is not None:
            for converter in self._converters:
                if converter.converter_id == self._document_converter_id or (
                    self._document_converter_id == "docling"
                    and converter.converter_id == "docling_local"
                ):
                    if converter.supports(content_type, source_path):
                        return converter
                    # Active converter doesn't support this content type
                    logger.info(
                        "Active converter '%s' does not support '%s'; "
                        "falling back to priority chain for %s",
                        self._document_converter_id, content_type, source_path.name,
                    )
                    break

            # If active converter is datalab and does not support this content type,
            # try the configured datalab_non_pdf_fallback before standard priority chain
            if self._document_converter_id == "datalab":
                fallback_target = self._datalab_non_pdf_fallback or "markitdown"
                for converter in self._converters:
                    if converter.converter_id == fallback_target or (
                        fallback_target == "docling" and converter.converter_id == "docling_local"
                    ):
                        if converter.supports(content_type, source_path):
                            logger.info(
                                "Item %s converted with '%s' (configured datalab non-pdf fallback from 'datalab')",
                                source_path.name, converter.converter_id,
                            )
                            return converter

        # Priority chain fallback
        for converter in self._converters:
            if converter.supports(content_type, source_path):
                if self._document_converter_id is not None:
                    logger.info(
                        "Item %s converted with '%s' (fallback from '%s')",
                        source_path.name, converter.converter_id,
                        self._document_converter_id,
                    )
                return converter

        return None

    def _run_converter(
            self,
            converter: BaseDocumentConverter,
            item_key: str,
            source_path: Path,
            content_type: str,
    ) -> str | None:
        """Execute a converter, creating the item bundle directory first."""
        logger.info(
            "Converting %s (%s) with '%s' ...",
            source_path.name, content_type, converter.converter_id,
        )
        item_dir = self._cache_dir / item_key
        item_dir.mkdir(parents=True, exist_ok=True)
        try:
            return converter.convert(item_key, source_path, content_type, item_dir)
        except Exception as exc:
            logger.error("Conversion failed for %s: %s", source_path, exc)
            return None

    def _load_from_cache(self, item_key: str, current_hash: str) -> str | None:
        """Return cached Markdown if the source file hasn't changed."""
        md_path = self._cache_dir / item_key / "document.md"
        if not md_path.exists():
            return None
        with _open_meta_db(self._cache_dir) as conn:
            row = conn.execute(
                "SELECT source_hash FROM conversion_meta WHERE item_key = ?",
                (item_key,),
            ).fetchone()
        if row and row["source_hash"] == current_hash:
            return md_path.read_text(encoding="utf-8")
        return None

    def _save_to_cache(
            self,
            item_key: str,
            source_path: Path,
            content_type: str,
            source_hash: str,
            markdown: str,
            converter_id: str,
            converter_version: str,
            parent_item_type: str | None = None,
            title: str | None = None,
            collections: list[Any] | None = None,
            parent_item_key: str | None = None,
            parse_quality_score: float | None = None,
            checkpoint_id: str | None = None,
            authors: list[str] | None = None,
            year: str | int | None = None,
            publication: str | None = None,
            doi: str | None = None,
            url: str | None = None,
            abstract: str | None = None,
            abstract_source: str | None = None,
            update_root_index: bool = True,
    ) -> str:
        """Write Markdown with frontmatter to disk, write catalogs, and upsert DB metadata.

        Args:
            item_key: Zotero item key.
            source_path: Original source file path (stored in metadata).
            content_type: MIME type of the source file.
            source_hash: SHA-256 hex digest of the source file at conversion time.
            markdown: Converted Markdown content to persist.
            converter_id: ID of the converter that produced the Markdown.
            converter_version: Version of the converter.
            parent_item_type: Zotero parent item type.
            title: Parent item title.
            collections: Parent item collections list.
            parent_item_key: Parent Zotero item key string.
            parse_quality_score: Optional parse quality score (0.0 to 5.0).
            checkpoint_id: Optional converter checkpoint ID.
            authors: Optional author names list.
            year: Optional publication year.
            publication: Optional publication venue string.
            doi: Optional DOI string.
            url: Optional source URL string.
            abstract: Optional abstract or summary text.
            abstract_source: Optional abstract source tag ('zotero', 'generated', 'extractive').
            update_root_index: If True (default), updates the root index.md catalog.

        Returns:
            Full document string including frontmatter.
        """
        item_dir = self._cache_dir / item_key
        item_dir.mkdir(parents=True, exist_ok=True)

        if parse_quality_score is None or checkpoint_id is None:
            try:
                conv = self._get_converter_by_id(converter_id)
                if hasattr(conv, "get_metadata"):
                    meta = conv.get_metadata(item_key)
                    if meta and isinstance(meta, dict):
                        if parse_quality_score is None:
                            parse_quality_score = meta.get("parse_quality_score")
                        if checkpoint_id is None:
                            checkpoint_id = meta.get("checkpoint_id")
            except Exception:
                pass

            if parse_quality_score is None or checkpoint_id is None:
                json_path = item_dir / "datalab_document.json"
                if json_path.exists():
                    try:
                        ast_data = json.loads(json_path.read_text(encoding="utf-8"))
                        if parse_quality_score is None and "parse_quality_score" in ast_data:
                            parse_quality_score = ast_data["parse_quality_score"]
                        if checkpoint_id is None and "checkpoint_id" in ast_data:
                            checkpoint_id = ast_data["checkpoint_id"]
                    except Exception:
                        pass

        markdown = wrap_standalone_markdown_tables(markdown)
        converted_at = datetime.now(UTC).isoformat()
        clean_md = strip_existing_frontmatter(markdown)

        # Defense-in-depth: rewrite any residual staging or absolute file:///.../images/ URIs
        clean_md = re.sub(
            r"file://[^\s\)\"']+?[/\\]images[/\\]([^\s\)\"']+)",
            lambda m: f"images/{urllib.parse.unquote(m.group(1))}",
            clean_md,
        )

        storage_rel_path = normalize_to_storage_relative(source_path)

        existing_keywords = None
        try:
            with _open_meta_db(self._cache_dir) as conn:
                existing_row = conn.execute(
                    "SELECT keywords FROM conversion_meta WHERE item_key = ?",
                    (item_key,),
                ).fetchone()
                if existing_row and existing_row["keywords"]:
                    existing_keywords = existing_row["keywords"]
        except Exception:
            pass

        keywords: list[str] = []
        if existing_keywords:
            try:
                parsed_kw = json.loads(existing_keywords)
                if isinstance(parsed_kw, list):
                    keywords = [str(k) for k in parsed_kw]
            except Exception:
                keywords = []

        if not keywords:
            try:
                from zocrates.rag.keyword_extractor import (
                    extract_statistical_keywords,
                    extract_structural_digest,
                )

                digest = extract_structural_digest(clean_md)
                keywords = extract_statistical_keywords(title or "", digest, max_keywords=6)
            except Exception as exc:
                logger.warning("Keyword extraction failed for %s: %s", item_key, exc)

        fm = build_frontmatter(
            item_key=item_key,
            parent_item_key=parent_item_key,
            parent_item_type=parent_item_type,
            title=title,
            collections=collections,
            source_path=storage_rel_path,
            source_hash=source_hash,
            content_type=content_type,
            converter_id=converter_id,
            converter_version=converter_version,
            converted_at=converted_at,
            parse_quality_score=parse_quality_score,
            checkpoint_id=checkpoint_id,
            authors=authors,
            year=year,
            publication=publication,
            doi=doi,
            url=url,
            keywords=keywords,
            bundle_dir=item_dir,
        )
        full_document = fm + clean_md

        md_path = item_dir / "document.md"
        md_path.write_text(full_document, encoding="utf-8")

        # Resolve abstract or generate summary
        effective_abstract = abstract.strip() if (abstract and str(abstract).strip()) else None
        effective_abstract_source = abstract_source
        if not effective_abstract:
            try:
                from zocrates.rag.summary_generator import generate_summary

                summary_text, src = generate_summary(clean_md, title=title)
                effective_abstract = summary_text
                effective_abstract_source = src
            except Exception as exc:
                logger.warning("Summary generation failed for %s: %s", item_key, exc)
                effective_abstract = "No abstract or summary available."
                effective_abstract_source = "extractive"
        elif not effective_abstract_source:
            effective_abstract_source = "zotero"

        effective_keywords = (
            existing_keywords if existing_keywords else (json.dumps(keywords) if keywords else None)
        )
        with _open_meta_db(self._cache_dir) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conversion_meta
                    (item_key, title, converter_id, converter_version, source_path,
                     content_type, parent_item_type, converted_at, source_hash, char_count,
                     parent_item_key, parse_quality_score, checkpoint_id, abstract, abstract_source, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_key,
                    title,
                    converter_id,
                    converter_version,
                    storage_rel_path,
                    content_type,
                    parent_item_type,
                    converted_at,
                    source_hash,
                    len(full_document),
                    parent_item_key,
                    parse_quality_score,
                    checkpoint_id,
                    effective_abstract,
                    effective_abstract_source,
                    effective_keywords,
                ),
            )

        # Generate initial toc.json and toc.md
        try:
            from zocrates.rag.toc_generator import generate_toc, save_toc_files

            toc_json, toc_md = generate_toc(
                document_md=full_document,
                title=title,
                abstract=effective_abstract,
                abstract_source=effective_abstract_source or "zotero",
                item_key=item_key,
                parent_item_key=parent_item_key,
            )
            save_toc_files(item_dir, toc_json, toc_md)
        except Exception as exc:
            logger.warning("Initial ToC generation failed for %s: %s", item_key, exc)

        try:
            regenerate_item_relations_md(item_key=item_key, cache_dir=self._cache_dir)
        except Exception as exc:
            logger.warning("Failed to generate item_relations.md for %s: %s", item_key, exc)
        generate_item_index_md(
            item_dir=item_dir,
            item_key=item_key,
            title=title,
            converter_id=converter_id,
            converter_version=converter_version,
        )
        if update_root_index:
            generate_root_index_md(self._cache_dir)

        logger.info(
            "Cached Markdown for %s (%d chars, converter=%s)",
            item_key, len(full_document), converter_id,
        )
        return full_document


def convert_note_html_to_markdown(html_content: str) -> str:
    """Convert Zotero note HTML content to clean, structured Markdown.

    Handles headings (h1–h6), paragraphs, ordered/unordered lists, blockquotes,
    pre/code blocks, tables, inline strong/em/code, anchors, and horizontal
    rules.  Falls back to basic regex stripping if BeautifulSoup is unavailable.
    """
    if not html_content or not html_content.strip():
        return ""

    import html as html_module

    try:
        from bs4 import BeautifulSoup, NavigableString, Tag

        soup = BeautifulSoup(html_content, "html.parser")

        def _node_to_md(node: Any) -> str:  # noqa: ANN401
            """Recursively convert a BeautifulSoup DOM node into markdown text.

            Args:
                node: NavigableString or Tag DOM node to convert.

            Returns:
                Markdown formatted string corresponding to the node.
            """
            if isinstance(node, NavigableString):
                return html_module.unescape(str(node))
            if not isinstance(node, Tag):
                return ""

            tag = node.name.lower() if node.name else ""
            children_md = "".join(_node_to_md(c) for c in node.children)

            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(tag[1])
                return f"\n\n{'#' * level} {children_md.strip()}\n\n"
            if tag == "p":
                return f"\n\n{children_md.strip()}\n\n"
            if tag == "br":
                return "\n"
            if tag in ("strong", "b"):
                inner = children_md.strip()
                return f"**{inner}**" if inner else ""
            if tag in ("em", "i"):
                inner = children_md.strip()
                return f"*{inner}*" if inner else ""
            if tag == "code":
                parent_tag = node.parent.name.lower() if node.parent and node.parent.name else ""
                if parent_tag == "pre":
                    return children_md  # <pre> wrapper handles fencing
                return f"`{children_md.strip()}`"
            if tag == "pre":
                inner = node.get_text()
                return f"\n\n```\n{inner.rstrip()}\n```\n\n"
            if tag == "blockquote":
                inner = children_md.strip()
                quoted = "\n".join(f"> {line}" for line in inner.splitlines())
                return f"\n\n{quoted}\n\n"
            if tag == "ul":
                return f"\n\n{children_md.strip()}\n\n"
            if tag == "ol":
                items_md: list[str] = []
                counter = 1
                for child in node.children:
                    if isinstance(child, Tag) and child.name.lower() == "li":
                        items_md.append(f"{counter}. {''.join(_node_to_md(gc) for gc in child.children).strip()}")
                        counter += 1
                return "\n\n" + "\n".join(items_md) + "\n\n"
            if tag == "li":
                parent_tag = node.parent.name.lower() if node.parent and node.parent.name else ""
                if parent_tag == "ol":
                    return children_md.strip()  # ol handler above owns numbering
                return f"- {children_md.strip()}\n"
            if tag == "table":
                rows: list[str] = []
                for tr in node.find_all("tr"):
                    cells = [
                        cell.get_text().strip().replace("|", "\\|")
                        for cell in tr.find_all(["th", "td"])
                    ]
                    if cells:
                        rows.append("| " + " | ".join(cells) + " |")
                if not rows:
                    return children_md
                col_count = rows[0].count("|") - 1
                sep = "| " + " | ".join(["---"] * col_count) + " |"
                rows.insert(1, sep)
                return "\n\n" + "\n".join(rows) + "\n\n"
            if tag == "a":
                href = node.get("href", "")
                label = children_md.strip()
                return f"[{label}]({href})" if href else label
            if tag in ("hr",):
                return "\n\n---\n\n"
            # Pass-through containers
            return children_md

        result = _node_to_md(soup)

    except Exception:
        result = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
        result = re.sub(r"</p>", "\n\n", result, flags=re.IGNORECASE)
        result = re.sub(r"</li>", "\n", result, flags=re.IGNORECASE)
        result = re.sub(r"<[^>]+>", "", result)
        result = html_module.unescape(result)

    lines = [line.rstrip() for line in result.splitlines()]
    clean_text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", clean_text)


def materialize_note_bundle(
    note: Any,
    syncer: Any,
    cache_dir: Path | None = None,
) -> tuple[Path, str]:
    """Create or refresh an OKF bundle for a Zotero note in the markdown cache.

    The bundle is placed at ``<cache_dir>/<note.note_key>/`` and contains
    ``document.md``, ``toc.json``, and ``toc.md``.  Staleness is detected via
    a SHA-256 hash of the raw HTML stored in ``conversion_meta.db``.

    Summary policy: **never calls an LLM**.
    - Notes < 100 plain-text words → no abstract.
    - Notes ≥ 100 plain-text words → deterministic extractive lead paragraph
      (first non-heading paragraph, capped at 300 characters).

    Thread-safe: uses a process-wide lock and an atomic directory swap
    (write to ``.tmp_{key}`` then rename).

    Args:
        note: A ``ZoteroNote`` dataclass instance.
        syncer: The active ``LibrarySyncer`` instance (provides ``processor``).
        cache_dir: Override for the cache directory; defaults to
            ``syncer.processor._cache_dir``.

    Returns:
        Tuple of ``(note_dir, note.note_key)``.
    """
    effective_cache_dir: Path = cache_dir if cache_dir is not None else syncer.processor._cache_dir
    note_dir = effective_cache_dir / note.note_key
    current_hash = hashlib.sha256(note.note_html.encode("utf-8")).hexdigest()

    # Fast path: bundle exists and HTML hasn't changed (checked outside lock).
    if note_dir.exists() and (note_dir / "document.md").exists():
        existing_meta = syncer.processor.get_metadata(note.note_key)
        if existing_meta and existing_meta.get("source_hash") == current_hash:
            return note_dir, note.note_key

    with _note_bundle_lock:
        # Re-check inside lock (double-checked locking pattern).
        if note_dir.exists() and (note_dir / "document.md").exists():
            existing_meta = syncer.processor.get_metadata(note.note_key)
            if existing_meta and existing_meta.get("source_hash") == current_hash:
                return note_dir, note.note_key

        title = (note.title or "Note").strip()
        markdown = convert_note_html_to_markdown(note.note_html)

        # Plain-text word count (strip all HTML tags for accurate count).
        plain_text = re.sub(r"<[^>]+>", " ", note.note_html)
        word_count = len(plain_text.split())

        # Strictly extractive summary — no LLM calls, ever.
        effective_abstract: str | None = None
        effective_abstract_source: str = "none"
        if word_count >= 100:
            paragraphs = [p.strip() for p in re.split(r"\n{2,}", markdown) if p.strip()]
            lead = ""
            for p in paragraphs:
                if not p.startswith("#"):  # skip heading lines
                    lead = p
                    break
            if lead:
                effective_abstract = lead[:300] + ("\u2026" if len(lead) > 300 else "")
                effective_abstract_source = "extractive"

        # Build YAML frontmatter.
        parent_key = note.parent_item_key or ""
        fm_lines = [
            "---",
            f'title: "{title}"',
            f'item_key: "{note.note_key}"',
            f'parent_item_key: "{parent_key}"',
            "type: note",
            "source:",
            f'  path: "zotero://note/{note.note_key}"',
            f'  hash: "{current_hash}"',
            '  content_type: "text/note"',
            "---",
        ]
        frontmatter = "\n".join(fm_lines) + "\n"
        full_doc = frontmatter + f"\n# {title}\n\n" + markdown

        # Write to a temp directory then atomically rename.
        tmp_dir = effective_cache_dir / f".tmp_{note.note_key}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        (tmp_dir / "document.md").write_text(full_doc, encoding="utf-8")

        try:
            from zocrates.rag.toc_generator import generate_toc, save_toc_files  # local import to avoid cycles

            toc_json, toc_md = generate_toc(
                document_md=full_doc,
                title=title,
                abstract=effective_abstract,
                abstract_source=effective_abstract_source,
                item_key=note.note_key,
                parent_item_key=note.parent_item_key,
            )
            save_toc_files(tmp_dir, toc_json, toc_md)
        except Exception as exc:
            logger.warning("ToC generation failed for note %s: %s", note.note_key, exc)

        # Generate item_relations.md and index.md inside staging directory before atomic rename
        try:
            regenerate_item_relations_md(
                item_key=note.note_key,
                cache_dir=effective_cache_dir,
                target_bundle_dir=tmp_dir,
            )
        except Exception as exc:
            logger.warning("Failed to generate item_relations.md for note %s: %s", note.note_key, exc)
        generate_item_index_md(
            item_dir=tmp_dir,
            item_key=note.note_key,
            title=title,
            converter_id="zotero_note_converter",
            converter_version="1.0",
        )

        # Atomic swap.
        if note_dir.exists():
            shutil.rmtree(note_dir, ignore_errors=True)
        tmp_dir.rename(note_dir)

        # Persist conversion metadata.
        existing_keywords = None
        with _open_meta_db(effective_cache_dir) as conn:
            try:
                existing_row = conn.execute(
                    "SELECT keywords FROM conversion_meta WHERE item_key = ?",
                    (note.note_key,),
                ).fetchone()
                if existing_row and existing_row["keywords"]:
                    existing_keywords = existing_row["keywords"]
            except sqlite3.OperationalError:
                pass

            converted_at = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT OR REPLACE INTO conversion_meta
                    (item_key, title, converter_id, converter_version, source_path,
                     content_type, parent_item_type, converted_at, source_hash, char_count,
                     parent_item_key, parse_quality_score, checkpoint_id, abstract, abstract_source, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_key,
                    title,
                    "zotero_note_converter",
                    "1.0",
                    f"zotero://note/{note.note_key}",
                    "text/note",
                    "note",
                    converted_at,
                    current_hash,
                    len(full_doc),
                    note.parent_item_key,
                    None,  # parse_quality_score
                    None,  # checkpoint_id
                    effective_abstract,
                    effective_abstract_source,
                    existing_keywords,
                ),
            )

        return note_dir, note.note_key


def compute_item_content_hash(item: Any) -> str:
    """Compute canonical content and bibliographic SHA-256 hash for a metadata-only Zotero item."""
    title = (getattr(item, "title", None) or "").strip()
    abstract = (getattr(item, "abstract", None) or "").strip()
    creators_str = ";".join(str(c) for c in (getattr(item, "creators", None) or []))
    collections_str = ";".join(
        f"{getattr(c, 'key', '')}:{getattr(c, 'name', '')}"
        for c in (getattr(item, "collections", None) or [])
    )
    raw = (
        f"{getattr(item, 'item_key', '')}|{getattr(item, 'item_type', '')}|{title}|"
        f"{creators_str}|{getattr(item, 'year', '')}|{getattr(item, 'publication', '')}|"
        f"{getattr(item, 'doi', '')}|{getattr(item, 'url', '')}|{abstract}|"
        f"{collections_str}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def materialize_item_metadata_bundle(
    item: Any,
    syncer: Any,
    cache_dir: Path | None = None,
    force: bool = False,
) -> tuple[Path, str]:
    """Create or refresh an OKF bundle for an attachment-free Zotero item in the markdown cache.

    The bundle is placed at ``<cache_dir>/<item.item_key>/`` and contains
    ``document.md``, ``toc.json``, ``toc.md``, ``index.md``, and ``item_relations.md``.
    Staleness is detected via a SHA-256 hash of the canonical title, abstract, and bibliographic fields.

    Zero LLM calls, thread-safe (via ``_metadata_bundle_lock``), and atomic directory
    swap (write to ``.tmp_{key}`` then rename).

    Args:
        item: A ``ZoteroItem`` instance.
        syncer: The active ``LibrarySyncer`` instance.
        cache_dir: Override for the cache directory; defaults to
            ``syncer.processor._cache_dir``.
        force: If True, bypasses hash fast-path and forces re-materialization.

    Returns:
        Tuple of ``(item_dir, item.item_key)``.
    """
    effective_cache_dir: Path = (
        cache_dir if cache_dir is not None else syncer.processor._cache_dir
    )
    item_dir = effective_cache_dir / item.item_key
    current_hash = compute_item_content_hash(item)

    # Fast path: bundle exists and hash matches (checked outside lock).
    if not force and item_dir.exists() and (item_dir / "document.md").exists():
        existing_meta = syncer.processor.get_metadata(item.item_key)
        if existing_meta and existing_meta.get("source_hash") == current_hash:
            return item_dir, item.item_key

    with _metadata_bundle_lock:
        # Re-check inside lock (double-checked locking pattern).
        if not force and item_dir.exists() and (item_dir / "document.md").exists():
            existing_meta = syncer.processor.get_metadata(item.item_key)
            if existing_meta and existing_meta.get("source_hash") == current_hash:
                return item_dir, item.item_key

        # Title fallback: if title is missing or whitespace-only
        raw_title = (getattr(item, "title", None) or "").strip()
        if not raw_title:
            effective_title = (
                getattr(item, "doi", None)
                or getattr(item, "url", None)
                or f"Untitled Item ({item.item_key})"
            ).strip()
        else:
            effective_title = raw_title

        # Abstract sanitization: strip JATS XML markup and demote raw markdown headers
        raw_abstract = (getattr(item, "abstract", None) or "").strip()
        cleaned_abstract: str | None = None
        if raw_abstract:
            # Normalize block tags to newlines then strip all HTML/JATS tags
            abs_text = re.sub(r"</(?:jats:[^>]+|p|div|sec|title)>", "\n", raw_abstract, flags=re.IGNORECASE)
            abs_text = re.sub(r"<[^>]+>", " ", abs_text)
            abs_text = html_module.unescape(abs_text)
            # Demote or strip raw markdown headings or YAML fences
            abs_lines = []
            for line in abs_text.splitlines():
                stripped_line = line.strip()
                if stripped_line.startswith("---"):
                    continue
                line = re.sub(r"^\s*#+\s*", "", line)
                line = re.sub(r"(?<=\s)#+\s*", "", line)
                abs_lines.append(line.rstrip())
            clean_str = "\n".join(abs_lines).strip()
            cleaned_abstract = re.sub(r"\n{3,}", "\n\n", clean_str).strip() or None

        # Statistical keywords extraction
        keywords: list[str] = []
        try:
            from zocrates.rag.keyword_extractor import extract_statistical_keywords

            keywords = extract_statistical_keywords(
                effective_title, cleaned_abstract or "", max_keywords=6
            )
        except Exception:
            pass

        # Build document body with Notice under ## Document Overview (outside the abstract)
        if cleaned_abstract:
            body = (
                f"\n# {effective_title}\n\n"
                "## Document Overview\n\n"
                "> **Notice**: This Zotero entry does not have an attached full-text document (PDF/HTML). "
                "Only bibliographic metadata and the abstract are available in the library.\n\n"
                "## Abstract\n\n"
                f"{cleaned_abstract}\n"
            )
        else:
            authors_list = getattr(item, "creators", None) or []
            authors_str = "; ".join(authors_list) if authors_list else "Unknown"
            year_val = getattr(item, "year", None) or "Unknown"
            pub_val = getattr(item, "publication", None) or ""
            doi_val = getattr(item, "doi", None) or ""
            summary_lines = [
                f"- **Title**: {effective_title}",
                f"- **Authors**: {authors_str}",
                f"- **Year**: {year_val}",
            ]
            if pub_val:
                summary_lines.append(f"- **Publication**: {pub_val}")
            if doi_val:
                summary_lines.append(f"- **DOI**: {doi_val}")
            bib_summary = "\n".join(summary_lines)

            body = (
                f"\n# {effective_title}\n\n"
                "## Document Overview\n\n"
                "> **Notice**: This Zotero entry is a metadata-only record with no attached "
                "full-text document (PDF/HTML) and no abstract in Zotero.\n\n"
                "## Bibliographic Summary\n\n"
                f"{bib_summary}\n"
            )

        from zocrates.rag.bundle_catalog import build_frontmatter

        converted_at = datetime.now(UTC).isoformat()
        frontmatter = build_frontmatter(
            item_key=item.item_key,
            parent_item_type=getattr(item, "item_type", None) or "document",
            title=effective_title,
            collections=getattr(item, "collections", None),
            source_path=f"zotero://item/{item.item_key}",
            source_hash=current_hash,
            content_type="text/zotero-metadata",
            converter_id="zotero_metadata",
            converter_version="1.0",
            converted_at=converted_at,
            parent_item_key=None,
            authors=getattr(item, "creators", None),
            year=getattr(item, "year", None),
            publication=getattr(item, "publication", None),
            doi=getattr(item, "doi", None),
            url=getattr(item, "url", None),
            keywords=keywords,
            bundle_dir=None,
        )
        full_doc = frontmatter + body

        # Write to staging directory then atomically swap
        tmp_dir = effective_cache_dir / f".tmp_{item.item_key}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        (tmp_dir / "document.md").write_text(full_doc, encoding="utf-8")

        # ToC Generation
        try:
            from zocrates.rag.toc_generator import generate_toc, save_toc_files

            effective_abstract = cleaned_abstract if cleaned_abstract else None
            effective_abstract_source = "zotero" if cleaned_abstract else "none"
            toc_json, toc_md = generate_toc(
                document_md=full_doc,
                title=effective_title,
                abstract=effective_abstract,
                abstract_source=effective_abstract_source,
                item_key=item.item_key,
                parent_item_key=None,
            )
            save_toc_files(tmp_dir, toc_json, toc_md)
        except Exception as exc:
            logger.warning(
                "ToC generation failed for metadata item %s: %s", item.item_key, exc
            )

        # item_relations.md & index.md inside staging dir
        try:
            from zocrates.rag.bundle_catalog import (
                generate_item_index_md,
                regenerate_item_relations_md,
            )

            regenerate_item_relations_md(
                item_key=item.item_key,
                cache_dir=effective_cache_dir,
                target_bundle_dir=tmp_dir,
            )
        except Exception as exc:
            logger.warning(
                "Failed to generate item_relations.md for metadata item %s: %s",
                item.item_key,
                exc,
            )

        generate_item_index_md(
            item_dir=tmp_dir,
            item_key=item.item_key,
            title=effective_title,
            converter_id="zotero_metadata",
            converter_version="1.0",
        )

        # Atomic swap
        if item_dir.exists():
            shutil.rmtree(item_dir, ignore_errors=True)
        tmp_dir.rename(item_dir)

        # Persist conversion metadata with indexed_at = NULL
        with _open_meta_db(effective_cache_dir) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conversion_meta
                    (item_key, title, converter_id, converter_version, source_path,
                     content_type, parent_item_type, converted_at, indexed_at, source_hash, char_count,
                     parent_item_key, parse_quality_score, checkpoint_id, abstract, abstract_source, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_key,
                    effective_title,
                    "zotero_metadata",
                    "1.0",
                    f"zotero://item/{item.item_key}",
                    "text/zotero-metadata",
                    getattr(item, "item_type", None) or "document",
                    converted_at,
                    None,  # indexed_at stamped upon vector store ingestion
                    current_hash,
                    len(full_doc),
                    None,  # parent_item_key
                    None,  # parse_quality_score
                    None,  # checkpoint_id
                    cleaned_abstract if cleaned_abstract else None,
                    "zotero" if cleaned_abstract else "none",
                    json.dumps(keywords) if keywords else None,
                ),
            )
            conn.commit()

        return item_dir, item.item_key
