"""
Frontmatter refresher and metadata synchronization engine for Zocrates.

Synchronizes OKF v0.2 YAML frontmatter in cached Markdown bundles with the
latest bibliographic metadata from Zotero (titles, authors, year, publication,
DOI, URL) without triggering PDF/HTML reconversion, OCR, or embedding models.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from zocrates.rag.bundle_catalog import (
    _FRONTMATTER_RE,
    build_frontmatter,
    generate_item_index_md,
    generate_root_index_md,
    resolve_source_path,
)
from zocrates.config import get_corpus_dir, get_fts_db_path
from zocrates.rag.document_processor import (
    _open_meta_db,
    sanitize_image_links_in_markdown,
)
from zocrates.rag.toc_generator import generate_toc, save_toc_files
from zocrates.rag.vector_store import VectorStore, _open_fts_db
from zocrates.rag.zotero_db import ZoteroDB

logger = logging.getLogger(__name__)

# Single-flight mutex protecting concurrent refresh sweeps (e.g. startup vs sync)
_REFRESH_LOCK = threading.Lock()


def refresh_bundle_frontmatter(
    zotero_dir: Path,
    target_keys: list[str] | None = None,
    dry_run: bool = False,
    sanitize_images: bool = False,
    store: VectorStore | None = None,
    cache_dir: Path | None = None,
) -> dict[str, int]:
    """Refresh YAML frontmatter and associated artifacts across target or all cached bundles.

    Args:
        zotero_dir: Root Zotero profile directory containing zotero.sqlite and storage/.
        target_keys: Optional list of attachment or note item keys to refresh.
            If None, scans all cached bundles in conversion_meta.db.
        dry_run: If True, previews changes without writing to disk or database.
        sanitize_images: If True, converts legacy file:/// URIs in document.md to images/.
        store: Optional existing VectorStore instance for FTS5 and Chroma updates.
        cache_dir: Optional explicit corpus directory. Defaults to
            zotero_dir / "zocrates-data" / "corpus".

    Returns:
        Summary dict containing counts of total, updated, skipped, and errored items.
    """
    if cache_dir is None:
        cache_dir = get_corpus_dir(zotero_dir)

    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    meta_db_path = cache_dir / "conversion_meta.db"

    if not sqlite_path.exists():
        logger.error("Zotero database not found at %s", sqlite_path)
        return {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    if not meta_db_path.exists():
        logger.info("No conversion_meta.db found at %s; nothing to refresh", meta_db_path)
        return {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    db = ZoteroDB(sqlite_path, storage_dir)

    with _open_meta_db(cache_dir) as meta_conn:
        if target_keys:
            placeholders = ",".join("?" for _ in target_keys)
            rows = meta_conn.execute(
                f"SELECT * FROM conversion_meta WHERE item_key IN ({placeholders})",
                target_keys,
            ).fetchall()
        else:
            rows = meta_conn.execute(
                "SELECT * FROM conversion_meta ORDER BY item_key"
            ).fetchall()

    if not rows:
        return {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    fts_path = store.fts_path if store is not None else get_fts_db_path(zotero_dir)
    fts_available = fts_path.exists()

    total = len(rows)
    updated = 0
    skipped = 0
    errored = 0
    noop_skipped_keys: list[str] = []

    with _REFRESH_LOCK:
        for r in rows:
            item_key = r["item_key"]
            if r["content_type"] in ("text/note", "text/zotero-metadata"):
                skipped += 1
                continue
            parent_key = r["parent_item_key"]
            bundle_dir = cache_dir / item_key
            doc_path = bundle_dir / "document.md"

            if not doc_path.exists():
                logger.warning("[%s] document.md not found on disk; skipping.", item_key)
                skipped += 1
                continue

            # Query fresh metadata from Zotero DB with single retry resilience
            parent_item = None
            try:
                if parent_key:
                    parent_item = db.get_item_by_key(parent_key)
                if not parent_item:
                    parent_item = db.get_item_by_key(item_key)
            except Exception as exc:
                time.sleep(0.25)
                try:
                    if parent_key:
                        parent_item = db.get_item_by_key(parent_key)
                    if not parent_item:
                        parent_item = db.get_item_by_key(item_key)
                except Exception as retry_exc:
                    logger.error(
                        "Failed to read Zotero metadata for item_key=%s: %s",
                        item_key,
                        retry_exc,
                    )
                    errored += 1
                    continue

            if parent_item is None:
                logger.warning(
                    "Item %s (parent %s) no longer found in Zotero library; skipping frontmatter refresh.",
                    item_key,
                    parent_key,
                )
                skipped += 1
                continue

            col_keys = r.keys()
            title = (
                parent_item.title
                if parent_item and parent_item.title
                else (r["title"] if "title" in col_keys and r["title"] else "")
            )
            p_type = parent_item.item_type if parent_item else r["parent_item_type"]
            colls = parent_item.collections if parent_item else []
            authors = parent_item.creators if parent_item else None
            year = parent_item.year if parent_item else None
            pub = parent_item.publication if parent_item else None
            doi = parent_item.doi if parent_item else None
            url = parent_item.url if parent_item else None
            abstract = (
                parent_item.abstract
                if parent_item and parent_item.abstract
                else (r["abstract"] if "abstract" in col_keys else None)
            )
            abstract_source = (
                "zotero"
                if (parent_item and parent_item.abstract)
                else (
                    r["abstract_source"]
                    if "abstract_source" in col_keys and r["abstract_source"]
                    else "extractive"
                )
            )

            raw_content = doc_path.read_text(encoding="utf-8")
            fm_match = _FRONTMATTER_RE.match(raw_content)
            if fm_match:
                old_fm_raw = fm_match.group(0)
                old_fm_lines = len(old_fm_raw.rstrip("\r\n").splitlines())
                body = raw_content[fm_match.end() :]
            else:
                old_fm_raw = ""
                old_fm_lines = 0
                body = raw_content

            if sanitize_images:
                body = sanitize_image_links_in_markdown(body)

            stored_path = r["source_path"] or ""
            resolved_src = resolve_source_path(stored_path, zotero_dir)
            fm_source_path = (
                stored_path
                if (
                    stored_path.startswith("storage/")
                    or stored_path.startswith("zotero://note/")
                    or stored_path.startswith("zotero://item/")
                )
                else (Path(resolved_src) if resolved_src else Path(stored_path))
            )

            keywords: list[str] | None = None
            if "keywords" in col_keys and r["keywords"]:
                try:
                    raw_kw = json.loads(r["keywords"])
                    if isinstance(raw_kw, list):
                        keywords = [str(k) for k in raw_kw if k]
                    elif isinstance(raw_kw, str):
                        keywords = [raw_kw]
                except Exception:
                    keywords = [k.strip() for k in str(r["keywords"]).split(",") if k.strip()]

            if keywords is None and fm_match:
                try:
                    parsed_yaml = yaml.safe_load(fm_match.group(1))
                    if isinstance(parsed_yaml, dict) and "keywords" in parsed_yaml:
                        yk = parsed_yaml["keywords"]
                        if isinstance(yk, list):
                            keywords = [str(k) for k in yk if k]
                        elif isinstance(yk, str):
                            keywords = [yk]
                except Exception:
                    pass

            new_fm = build_frontmatter(
                item_key=item_key,
                parent_item_type=p_type,
                title=title,
                collections=colls,
                source_path=fm_source_path,
                source_hash=r["source_hash"],
                content_type=r["content_type"],
                converter_id=r["converter_id"],
                converter_version=r["converter_version"],
                converted_at=r["converted_at"],
                parent_item_key=parent_key,
                parse_quality_score=r["parse_quality_score"]
                if "parse_quality_score" in col_keys
                else None,
                checkpoint_id=r["checkpoint_id"]
                if "checkpoint_id" in col_keys
                else None,
                authors=authors,
                year=year,
                publication=pub,
                doi=doi,
                url=url,
                keywords=keywords,
                bundle_dir=bundle_dir,
            )

            # Deterministic No-Op check
            new_fm_normalized = new_fm.replace("\r\n", "\n")
            old_fm_normalized = old_fm_raw.replace("\r\n", "\n")
            body_changed = (
                sanitize_images
                and fm_match
                and body != raw_content[fm_match.end() :]
            )

            # Self-healing check: verify if toc.json exists and matches current frontmatter line count
            toc_json_path = bundle_dir / "toc.json"
            toc_out_of_sync = False
            if not toc_json_path.exists():
                toc_out_of_sync = True
            else:
                try:
                    with open(toc_json_path, encoding="utf-8") as tf:
                        cached_toc = json.load(tf)
                    secs = cached_toc.get("sections", [])
                    if secs and secs[0].get("id") == "s0":
                        if secs[0].get("end_line") != old_fm_lines:
                            toc_out_of_sync = True
                except Exception:
                    toc_out_of_sync = True

            # Self-healing check: verify if chunk_meta has drifted Chunk 0 line boundary
            chunk_meta_out_of_sync = False
            if fts_available:
                try:
                    with _open_fts_db(fts_path) as conn:
                        cm_row = conn.execute(
                            "SELECT chunk_type, line_end FROM chunk_meta WHERE item_key = ? AND chunk_index = 0",
                            (item_key,),
                        ).fetchone()
                        if cm_row and cm_row["chunk_type"] == "frontmatter":
                            if cm_row["line_end"] is not None and cm_row["line_end"] != old_fm_lines:
                                chunk_meta_out_of_sync = True
                except Exception:
                    pass

            if (
                new_fm_normalized == old_fm_normalized
                and not body_changed
                and not toc_out_of_sync
                and not chunk_meta_out_of_sync
            ):
                if not dry_run:
                    noop_skipped_keys.append(item_key)
                skipped += 1
                continue

            if dry_run:
                updated += 1
                continue

            new_fm_lines = len(new_fm.rstrip("\r\n").splitlines())
            delta_l = new_fm_lines - old_fm_lines
            full_markdown = new_fm + body

            # 1. Crash-safe atomic write of document.md
            tmp_doc = bundle_dir / "document.md.tmp"
            try:
                with open(tmp_doc, "w", encoding="utf-8") as f:
                    f.write(full_markdown)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_doc, doc_path)
            except Exception as write_exc:
                logger.error("Failed atomic write for %s: %s", doc_path, write_exc)
                if tmp_doc.exists():
                    tmp_doc.unlink(missing_ok=True)
                errored += 1
                continue

            # 2. Synchronize ToC
            try:
                toc_json, toc_md = generate_toc(
                    document_md=full_markdown,
                    title=title,
                    abstract=abstract,
                    abstract_source=abstract_source,
                    item_key=item_key,
                    parent_item_key=parent_key,
                )
                save_toc_files(bundle_dir, toc_json, toc_md)
            except Exception as toc_exc:
                logger.warning("Failed to regenerate ToC for %s: %s", item_key, toc_exc)

            # 3. Synchronize chunk_meta and chunks_fts in SQLite FTS5
            is_fm_chunk = True
            fts_succeeded = True
            if fts_available:
                try:
                    with _open_fts_db(fts_path) as conn:
                        c0_row = conn.execute(
                            "SELECT chunk_type, line_end FROM chunk_meta WHERE item_key = ? AND chunk_index = 0",
                            (item_key,),
                        ).fetchone()
                        is_fm_chunk = (c0_row is None) or (c0_row["chunk_type"] == "frontmatter")

                        if c0_row and c0_row["chunk_type"] == "frontmatter" and c0_row["line_end"] is not None:
                            shift_amount = new_fm_lines - c0_row["line_end"]
                        else:
                            shift_amount = delta_l

                        if is_fm_chunk:
                            # Update Chunk 0 line boundary
                            conn.execute(
                                "UPDATE chunk_meta SET line_end = ? WHERE item_key = ? AND chunk_index = 0",
                                (new_fm_lines, item_key),
                            )
                            # Idempotently replace Chunk 0 text in chunks_fts
                            conn.execute(
                                "DELETE FROM chunks_fts WHERE chunk_id = ?",
                                (f"{item_key}::0",),
                            )
                            conn.execute(
                                "INSERT INTO chunks_fts (chunk_id, item_key, chunk_text) VALUES (?, ?, ?)",
                                (f"{item_key}::0", item_key, new_fm),
                            )

                        # Shift body chunks by shift_amount (derived from stored Chunk 0 line_end for self-healing)
                        if shift_amount != 0:
                            conn.execute(
                                "UPDATE chunk_meta SET line_start = line_start + ?, line_end = line_end + ? "
                                "WHERE item_key = ? AND chunk_index > 0",
                                (shift_amount, shift_amount, item_key),
                            )
                        conn.commit()
                except Exception as fts_exc:
                    fts_succeeded = False
                    logger.warning(
                        "Failed to update FTS5/chunk_meta for %s: %s",
                        item_key,
                        fts_exc,
                    )

            # 4. Chroma Chunk 0 Text & Title Propagation (Zero Re-Embedding)
            if store is not None:
                try:
                    if is_fm_chunk:
                        store.update_chunk_text_preserve_vector(f"{item_key}::0", new_fm)
                    store.update_metadata(item_key, {"document_title": title})
                except Exception as chroma_exc:
                    logger.warning(
                        "Failed to update Chroma metadata for %s: %s",
                        item_key,
                        chroma_exc,
                    )

            # 5. Update conversion_meta.db (only record updated_at if FTS succeeded)
            new_char_count = len(full_markdown)
            now_ts = datetime.now(timezone.utc).isoformat()
            try:
                with _open_meta_db(cache_dir) as meta_conn:
                    if fts_succeeded:
                        meta_conn.execute(
                            "UPDATE conversion_meta SET title = ?, char_count = ?, updated_at = ? WHERE item_key = ?",
                            (title, new_char_count, now_ts, item_key),
                        )
                    else:
                        meta_conn.execute(
                            "UPDATE conversion_meta SET title = ?, char_count = ? WHERE item_key = ?",
                            (title, new_char_count, item_key),
                        )
                    if abstract:
                        meta_conn.execute(
                            "UPDATE conversion_meta SET abstract = ? WHERE item_key = ? AND (abstract IS NULL OR abstract = '')",
                            (abstract, item_key),
                        )
                        if "abstract_source" in col_keys:
                            meta_conn.execute(
                                "UPDATE conversion_meta SET abstract_source = ? WHERE item_key = ? AND (abstract_source IS NULL OR abstract_source = '')",
                                (abstract_source, item_key),
                            )
                    if keywords and "keywords" in col_keys and not r["keywords"]:
                        meta_conn.execute(
                            "UPDATE conversion_meta SET keywords = ? WHERE item_key = ? AND (keywords IS NULL OR keywords = '')",
                            (json.dumps(keywords), item_key),
                        )
                    meta_conn.commit()
            except Exception as meta_exc:
                logger.warning(
                    "Failed to update conversion_meta for %s: %s",
                    item_key,
                    meta_exc,
                )

            # 6. Item-level catalog
            try:
                generate_item_index_md(
                    bundle_dir,
                    item_key,
                    title,
                    r["converter_id"],
                    r["converter_version"],
                )
            except Exception as idx_exc:
                logger.warning(
                    "Failed to regenerate index.md for %s: %s",
                    item_key,
                    idx_exc,
                )

            updated += 1

        # Batch update updated_at for no-op skipped items in a single transaction
        if noop_skipped_keys and not dry_run:
            try:
                now_ts = datetime.now(timezone.utc).isoformat()
                with _open_meta_db(cache_dir) as meta_conn:
                    meta_conn.executemany(
                        "UPDATE conversion_meta SET updated_at = ? WHERE item_key = ?",
                        [(now_ts, k) for k in noop_skipped_keys],
                    )
                    meta_conn.commit()
            except Exception as batch_exc:
                logger.debug("Failed to batch update updated_at for skipped items: %s", batch_exc)

    # 7. Batched root catalog regeneration
    if not dry_run and updated > 0:
        try:
            generate_root_index_md(cache_dir)
        except Exception as root_exc:
            logger.warning("Failed to regenerate root index.md: %s", root_exc)

    return {"total": total, "updated": updated, "skipped": skipped, "errored": errored}


def sync_stale_metadata(
    zotero_dir: Path,
    dry_run: bool = False,
    sanitize_images: bool = False,
    store: VectorStore | None = None,
    cache_dir: Path | None = None,
) -> dict[str, int]:
    """Scan all cached items and refresh any whose Zotero metadata has been modified.

    Performs a lightweight timestamp comparison (<5ms) between conversion_meta.db
    and zotero.sqlite. Only items whose source metadata was modified since
    conversion are dispatched to refresh_bundle_frontmatter.

    Args:
        zotero_dir: Root Zotero profile directory.
        dry_run: If True, previews changes without writing to disk or database.
        sanitize_images: If True, sanitizes legacy file:/// image links.
        store: Optional existing VectorStore instance for Chroma and FTS updates.
        cache_dir: Optional explicit markdown cache directory.

    Returns:
        Summary dict containing counts of total, updated, skipped, and errored items.
        When stale items are found, total reflects the count of stale items dispatched
        to refresh_bundle_frontmatter; when no items are stale, total and skipped reflect
        the full count of checked cached bundles.
    """
    if cache_dir is None:
        cache_dir = get_corpus_dir(zotero_dir)

    meta_db = cache_dir / "conversion_meta.db"
    if not meta_db.exists():
        return {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    with _open_meta_db(cache_dir) as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversion_meta)").fetchall()
        }
        if "updated_at" in cols:
            rows = conn.execute(
                "SELECT item_key, parent_item_key, COALESCE(updated_at, converted_at) AS converted_at "
                "FROM conversion_meta"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT item_key, parent_item_key, converted_at FROM conversion_meta"
            ).fetchall()

    if not rows:
        return {"total": 0, "updated": 0, "skipped": 0, "errored": 0}

    key_to_atts: dict[str, list[str]] = {}
    converted_map: dict[str, str] = {}
    for r in rows:
        target_key = r["parent_item_key"] or r["item_key"]
        key_to_atts.setdefault(target_key, []).append(r["item_key"])
        converted_map[r["item_key"]] = r["converted_at"]

    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    if not sqlite_path.exists():
        return {"total": len(rows), "updated": 0, "skipped": len(rows), "errored": 0}

    db = ZoteroDB(sqlite_path, storage_dir)
    stale_keys = db.get_stale_item_keys(converted_map, key_to_atts)

    if not stale_keys:
        return {"total": len(rows), "updated": 0, "skipped": len(rows), "errored": 0}

    return refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        target_keys=stale_keys,
        dry_run=dry_run,
        sanitize_images=sanitize_images,
        store=store,
        cache_dir=cache_dir,
    )
