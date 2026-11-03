"""
Background ingestion worker.

Coordinates the document processing pipeline:
ZoteroDB → DocumentProcessor → Chunker → VectorStore
"""

import asyncio
import gc
import logging
import time
from collections.abc import Callable
from pathlib import Path

from zocrates.rag.chunker import chunk_markdown
from zocrates.config import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    DEFAULT_SYNC_AUTO_ABORT_FAILURES,
    get_chroma_dir,
    get_corpus_dir,
    get_datalab_non_pdf_fallback,
    get_document_converter_id,
    get_fts_db_path,
    get_sync_auto_abort_failures,
)
from zocrates.rag.document_processor import (
    DocumentProcessor,
    convert_note_html_to_markdown,
    materialize_note_bundle,
)
from zocrates.rag.embeddings import get_embedder
from zocrates.rag.vector_store import VectorStore
from zocrates.rag.zotero_db import ZoteroAttachment, ZoteroDB, ZoteroNote

logger = logging.getLogger(__name__)


class LibrarySyncer:
    """
    Coordinates RAG indexing pipeline.
    """

    def __init__(self, zotero_data_dir: Path, sqlite_path: Path) -> None:
        """Initialize the LibrarySyncer with all RAG pipeline components.

        Args:
            zotero_data_dir: Root Zotero data directory containing
                ``zotero.sqlite`` and the ``storage/`` subdirectory.
            sqlite_path: Absolute path to ``zotero.sqlite``.
        """
        self._zotero_data_dir = zotero_data_dir
        storage_dir = zotero_data_dir / "storage"

        self.db = ZoteroDB(sqlite_path, storage_dir)

        md_cache = get_corpus_dir(zotero_data_dir)
        active_converter = get_document_converter_id(zotero_data_dir)
        datalab_fallback = get_datalab_non_pdf_fallback(zotero_data_dir)
        self.processor = DocumentProcessor(
            md_cache,
            document_converter_id=active_converter,
            datalab_non_pdf_fallback=datalab_fallback,
        )

        embedder = get_embedder()
        self.store = VectorStore(
            chroma_dir=get_chroma_dir(zotero_data_dir),
            fts_db_path=get_fts_db_path(zotero_data_dir),
            embedder=embedder,
        )

        self._running = False
        self.active_context_keys = None

    # ---- public API --------------------------------------------------------

    def purge_item(self, item_key: str, regenerate_index: bool = True) -> None:
        """Atomically purge an item across all four datastores: disk bundle, metadata DB, vector/FTS store, and root index."""
        if not item_key:
            return
        self.processor.delete_cached(item_key, update_root_index=False)
        self.store.delete_document(item_key)
        if regenerate_index:
            self.processor.regenerate_root_index()

    def sync_metadata_staleness(self) -> dict[str, int]:
        """Scan cached bundles and synchronize frontmatter and FTS5 for any modified in Zotero.

        Executes a <5ms batched staleness check comparing conversion_meta.db against
        zotero.sqlite. If items are stale, updates their YAML frontmatter, toc.json,
        chunks_fts Chunk 0, and Chroma metadata without re-running document converters
        or OCR engines.

        Returns:
            Summary dict containing counts of total, updated, skipped, and errored items.
        """
        from zocrates.rag.frontmatter_refresher import sync_stale_metadata
        md_cache = get_corpus_dir(self._zotero_data_dir)
        res = sync_stale_metadata(
            zotero_dir=self._zotero_data_dir,
            store=self.store,
            cache_dir=md_cache,
        )

        # Sync-time janitor: purge headless or deleted items on backend startup
        try:
            all_known = (
                set(self.processor.list_cached_keys())
                | self.processor.get_all_meta_keys()
                | self.store.get_all_item_keys()
            )
            if all_known and self.db:
                active_keys = self.db.get_active_item_keys(list(all_known))
                if not active_keys:
                    logger.warning("Skipping startup orphan purge: no active items found in database")
                else:
                    orphans = all_known - active_keys
                    if orphans:
                        logger.info("Cleaning up %d orphaned items during staleness sync...", len(orphans))
                        for o_key in orphans:
                            self.purge_item(o_key, regenerate_index=False)
                        self.processor.regenerate_root_index()
                        res["orphans_cleaned"] = len(orphans)
        except Exception as exc:
            logger.warning("Error checking orphans during staleness sync: %s", exc)

        return res

    async def full_sync(
        self,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        """Scan every item, note, and standalone attachment in the library and ingest those not yet indexed.

        Additive Phase 7 onboarding parameters (backward compatible):

        Args:
            progress_callback: Optional synchronous callback invoked at each
                item boundary with a progress dict (``stage``, ``processed``,
                ``skipped``, ``errors``, ``total``, ``current_title``,
                ``elapsed_ms``). Callback failures are logged and never abort
                the sync (fail-open).
            cancel_check: Optional cooperative-cancel predicate polled at the
                existing per-item yield points. When it returns True the sync
                finishes the current item and stops, skipping the tail
                graph-relations/orphan-purge phases. Resume semantics are
                untouched (``store.is_ingested`` keying + SHA-256 conversion
                cache).
        """
        sync_started = time.perf_counter()

        def _report_progress(stage: str, current_title: str = "") -> None:
            """Invoke the progress callback with current sync progress metrics.

            Args:
                stage: Current stage of synchronization (e.g. 'syncing', 'indexing').
                current_title: Title of the document currently being processed.
            """
            if progress_callback is None:
                return
            try:
                progress_callback({
                    "stage": stage,
                    "processed": processed,
                    "skipped": skipped,
                    "errors": errors,
                    "total": total,
                    "current_title": current_title,
                    "elapsed_ms": int((time.perf_counter() - sync_started) * 1000),
                })
            except Exception as exc:
                logger.warning("Sync progress callback failed: %s", exc)

        def _cancel_requested() -> bool:
            """Check if cooperative cancellation was requested for the sync job.

            Returns:
                True if cancellation was requested, False otherwise.
            """
            if cancel_check is None:
                return False
            try:
                return bool(cancel_check())
            except Exception as exc:
                logger.warning("Sync cancel_check failed: %s", exc)
                return False

        try:
            auto_abort_threshold = int(get_sync_auto_abort_failures(self._zotero_data_dir))
        except (ValueError, TypeError):
            auto_abort_threshold = DEFAULT_SYNC_AUTO_ABORT_FAILURES
        consecutive_failures = 0
        cancelled = False
        auto_aborted = False
        # Synchronize metadata for existing bundles modified in Zotero
        try:
            staleness_res = await asyncio.to_thread(self.sync_metadata_staleness)
            logger.info("Metadata staleness sync in full_sync complete: %s", staleness_res)
        except Exception as exc:
            logger.warning("Error during metadata staleness sync in full_sync: %s", exc)
            staleness_res = {"updated": 0}

        items = self.db.get_all_items()
        standalone_notes = self.db.get_standalone_notes()
        standalone_attachments = self.db.get_standalone_attachments()

        total = len(items) + len(standalone_notes) + len(standalone_attachments)
        processed = 0
        skipped = 0
        errors = 0
        # Emit an initial snapshot immediately so SSE subscribers get the
        # determinate total before the first (potentially multi-minute)
        # conversion finishes.
        _report_progress("starting")

        logger.info(
            "Starting full library sync: %d regular items, %d standalone notes, %d standalone attachments found.",
            len(items),
            len(standalone_notes),
            len(standalone_attachments),
        )

        # Auto-backfill missing parent_item_key values in conversion_meta.db
        all_atts = self.db.get_all_attachments()
        for att in all_atts:
            if att.parent_item_key:
                self.processor.ensure_parent_item_key(att.item_key, att.parent_item_key)

        for item in items:
            try:
                result = await self._ingest_item(
                    item.item_id, item.item_key, item.title
                )
                if result == "processed":
                    processed += 1
                else:
                    skipped += 1
                consecutive_failures = 0
            except Exception as exc:
                logger.error(
                    "Error ingesting item %s: %s", item.item_key, exc, exc_info=True
                )
                errors += 1
                consecutive_failures += 1
            await asyncio.sleep(0)
            _report_progress("items", item.title)
            if _cancel_requested():
                cancelled = True
                break
            if auto_abort_threshold > 0 and consecutive_failures >= auto_abort_threshold:
                logger.warning(
                    "Auto-aborting full sync after %d consecutive failures (threshold: %d).",
                    consecutive_failures,
                    auto_abort_threshold,
                )
                auto_aborted = True
                break

        for note in standalone_notes:
            try:
                result = await self._ingest_standalone_note(note)
                if result == "processed":
                    processed += 1
                else:
                    skipped += 1
                consecutive_failures = 0
            except Exception as exc:
                logger.error(
                    "Error ingesting standalone note %s: %s", note.note_key, exc, exc_info=True
                )
                errors += 1
                consecutive_failures += 1
            await asyncio.sleep(0)
            _report_progress("notes", getattr(note, "title", "") or "")
            if _cancel_requested():
                cancelled = True
                break
            if auto_abort_threshold > 0 and consecutive_failures >= auto_abort_threshold:
                logger.warning(
                    "Auto-aborting full sync after %d consecutive failures (threshold: %d).",
                    consecutive_failures,
                    auto_abort_threshold,
                )
                auto_aborted = True
                break

        for att in standalone_attachments:
            try:
                result = await self._ingest_standalone_attachment(att)
                if result == "processed":
                    processed += 1
                else:
                    skipped += 1
                consecutive_failures = 0
            except Exception as exc:
                logger.error(
                    "Error ingesting standalone attachment %s: %s", att.item_key, exc, exc_info=True
                )
                errors += 1
                consecutive_failures += 1
            await asyncio.sleep(0)
            _report_progress("attachments", getattr(att, "title", "") or "")
            if _cancel_requested():
                cancelled = True
                break
            if auto_abort_threshold > 0 and consecutive_failures >= auto_abort_threshold:
                logger.warning(
                    "Auto-aborting full sync after %d consecutive failures (threshold: %d).",
                    consecutive_failures,
                    auto_abort_threshold,
                )
                auto_aborted = True
                break

        # Pass of manual relations and shared tags synchronization
        # (skipped on cooperative cancel / auto-abort — the next successful
        # /sync executes it; plan §6.3 tail-phase semantics)
        meta_db_path = self.processor._cache_dir / "conversion_meta.db"
        if meta_db_path.exists() and not cancelled and not auto_aborted:
            try:
                def _sync_graph_relations_worker() -> None:
                    """Sync human-curated relations, shared tags, and regenerate bundle markdown."""
                    from zocrates.rag.bundle_catalog import regenerate_item_relations_md
                    from zocrates.config import get_graph_relations_config
                    from zocrates.rag.document_processor import _open_meta_db
                    from zocrates.rag.graph_engine import sync_manual_relations, sync_shared_tags

                    cfg = get_graph_relations_config(self._zotero_data_dir)
                    if not cfg.get("enabled", True):
                        return

                    with _open_meta_db(self.processor._cache_dir) as meta_conn:
                        n_manual = 0
                        n_tags = 0
                        if cfg.get("include_human_curated", True):
                            n_manual = sync_manual_relations(meta_conn, self._zotero_data_dir)
                        if cfg.get("include_shared_tags", True):
                            n_tags = sync_shared_tags(meta_conn, self._zotero_data_dir)

                        if (n_manual > 0 or n_tags > 0) and cfg.get("sync_bundle_files", True):
                            rows = meta_conn.execute("SELECT item_key FROM conversion_meta").fetchall()
                            for r in rows:
                                try:
                                    regenerate_item_relations_md(r["item_key"], self.processor._cache_dir)
                                except Exception as exc_inner:
                                    logger.warning(
                                        "Failed to regenerate item_relations.md for %s during full_sync: %s",
                                        r["item_key"],
                                        exc_inner,
                                    )

                await asyncio.to_thread(_sync_graph_relations_worker)
            except Exception as exc:
                logger.warning(
                    "Error syncing manual relations or shared tags in full_sync: %s",
                    exc,
                )

        # Orphan detection and cleanup (skipped on cooperative cancel /
        # auto-abort — same tail-phase semantics as above)
        orphaned_keys: set[str] = set()
        if not cancelled and not auto_aborted:
            active_keys: set[str] = set()
            for item in items:
                active_keys.add(item.item_key)
                for att in self.db.get_attachments(item.item_id):
                    active_keys.add(att.item_key)
                for note in self.db.get_notes_for_item(item.item_id):
                    active_keys.add(note.note_key)
            for note in standalone_notes:
                active_keys.add(note.note_key)
            for att in standalone_attachments:
                active_keys.add(att.item_key)

            all_known_keys = (
                set(self.processor.list_cached_keys())
                | self.processor.get_all_meta_keys()
                | self.store.get_all_item_keys()
            )
            if not active_keys:
                logger.warning("Skipping full sync orphan purge: no active items found in database")
            else:
                orphaned_keys = all_known_keys - active_keys

            if orphaned_keys:
                logger.info("Cleaning up %d orphaned cached items...", len(orphaned_keys))
                for o_key in orphaned_keys:
                    self.purge_item(o_key, regenerate_index=False)

        summary = {
            "total": total,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "orphans_cleaned": len(orphaned_keys),
            "metadata_refreshed": staleness_res.get("updated", 0),
            "cancelled": cancelled,
            "auto_aborted": auto_aborted,
        }
        self.processor.regenerate_root_index()
        logger.info("Full sync complete: %s", summary)
        return summary

    async def ingest_items(self, item_ids: list[int]) -> dict:
        """Force-ingest a list of item, note, or attachment IDs (e.g. newly added by Zotero)."""
        items = self.db.get_items_by_ids(item_ids)
        processed = 0
        errors = 0

        for item in items:
            try:
                if item.item_type == "note":
                    note = self.db.get_note_by_id(item.item_id)
                    if note:
                        await self._ingest_standalone_note(note, force=True)
                        processed += 1
                elif item.item_type == "attachment":
                    att = self.db.get_attachment_by_id(item.item_id)
                    if att:
                        await self._ingest_standalone_attachment(att, force=True)
                        processed += 1
                else:
                    await self._ingest_item(
                        item.item_id, item.item_key, item.title, force=True
                    )
                    processed += 1
            except Exception as exc:
                logger.error(
                    "Error ingesting item %s: %s", item.item_key, exc, exc_info=True
                )
                errors += 1
            await asyncio.sleep(0)

        summary = {"requested": len(item_ids), "processed": processed, "errors": errors}
        self.processor.regenerate_root_index()
        return summary

    # ---- private -----------------------------------------------------------

    async def _ingest_item(
        self, item_id: int, item_key: str, document_title: str, force: bool = False
    ) -> str:
        """Convert and index one Zotero item along with its attached child notes."""
        attachments = self.db.get_attachments(item_id)
        child_notes = self.db.get_notes_for_item(item_id)

        if not attachments and not child_notes:
            return "skipped"

        loop = asyncio.get_event_loop()
        item_processed = False

        # 1. Ingest paper PDF/HTML/all attachments present
        if attachments:
            parent_item = self.db.get_item(item_id)
            p_type = parent_item.item_type if parent_item else None
            p_title = parent_item.title if parent_item else document_title
            p_colls = parent_item.collections if parent_item else None
            p_authors = parent_item.creators if parent_item else None
            p_year = parent_item.year if parent_item else None
            p_pub = parent_item.publication if parent_item else None
            p_doi = parent_item.doi if parent_item else None
            p_url = parent_item.url if parent_item else None
            p_abstract = parent_item.abstract if parent_item else None

            for attachment in attachments:
                if not attachment.path or not attachment.path.exists():
                    continue

                if not force and self.store.is_ingested(attachment.item_key):
                    if item_key:
                        self.processor.ensure_parent_item_key(attachment.item_key, item_key)
                    # Lazy backfill ToC files if missing from existing cached bundle
                    bundle_dir = self.processor._cache_dir / attachment.item_key
                    if bundle_dir.exists() and (bundle_dir / "document.md").exists():
                        if not (bundle_dir / "toc.json").exists() or not (bundle_dir / "toc.md").exists():
                            try:
                                from zocrates.rag.bundle_catalog import generate_item_index_md
                                from zocrates.rag.toc_generator import generate_toc, save_toc_files

                                doc_text = (bundle_dir / "document.md").read_text(encoding="utf-8")
                                meta = self.processor.get_metadata(attachment.item_key)
                                abs_text = (meta.get("abstract") if meta else None) or p_abstract
                                abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
                                t_json, t_md = generate_toc(
                                    doc_text,
                                    title=p_title,
                                    abstract=abs_text,
                                    abstract_source=abs_src,
                                    item_key=attachment.item_key,
                                    parent_item_key=attachment.parent_item_key,
                                )
                                save_toc_files(bundle_dir, t_json, t_md)

                                # Persist abstract to conversion_meta if missing or backfilled
                                if abs_text and meta and (not meta.get("abstract") or not meta.get("abstract_source")):
                                    self.processor.update_abstract(attachment.item_key, abs_text, abs_src)

                                # Refresh index.md catalog
                                conv_id = meta.get("converter_id", "markitdown") if meta else "markitdown"
                                conv_ver = meta.get("converter_version", "1.0") if meta else "1.0"
                                generate_item_index_md(
                                    item_dir=bundle_dir,
                                    item_key=attachment.item_key,
                                    title=p_title,
                                    converter_id=conv_id,
                                    converter_version=conv_ver,
                                )
                            except Exception as exc:
                                logger.warning("Lazy ToC generation failed for %s: %s", attachment.item_key, exc)
                    continue

                markdown = await loop.run_in_executor(
                    None,
                    lambda att=attachment, pt=p_type, ti=p_title, co=p_colls, pk=item_key, auth=p_authors, yr=p_year, pub=p_pub, doi=p_doi, url=p_url, ab=p_abstract: (
                        self.processor.get_or_convert(
                            att.item_key,
                            att.path,
                            att.content_type,
                            parent_item_type=pt,
                            title=ti,
                            collections=co,
                            parent_item_key=pk,
                            authors=auth,
                            year=yr,
                            publication=pub,
                            doi=doi,
                            url=url,
                            abstract=ab,
                            update_root_index=False,
                        )
                    ),
                )
                if markdown and markdown.strip():
                    try:
                        from zocrates.rag.vlm_captioner import enrich_document_bundle

                        bundle_dir = self.processor._cache_dir / attachment.item_key
                        enriched = await loop.run_in_executor(
                            None,
                            enrich_document_bundle,
                            bundle_dir,
                            self._zotero_data_dir,
                        )
                        if enriched:
                            markdown = enriched
                    except Exception as exc:
                        logger.warning(
                            "VLM figure enrichment failed for %s: %s; proceeding with base markdown",
                            attachment.item_key,
                            exc,
                        )

                    # Update ToC on final post-enrichment markdown
                    try:
                        from zocrates.rag.toc_generator import generate_toc, save_toc_files

                        bundle_dir = self.processor._cache_dir / attachment.item_key
                        meta = self.processor.get_metadata(attachment.item_key)
                        abs_text = (meta.get("abstract") if meta else None) or p_abstract
                        abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
                        t_json, t_md = generate_toc(
                            markdown,
                            title=p_title,
                            abstract=abs_text,
                            abstract_source=abs_src,
                            item_key=attachment.item_key,
                            parent_item_key=attachment.parent_item_key,
                        )
                        save_toc_files(bundle_dir, t_json, t_md)
                    except Exception as exc:
                        logger.warning(
                            "Post-enrichment ToC generation failed for %s: %s",
                            attachment.item_key,
                            exc,
                        )

                    chunks = chunk_markdown(
                        markdown,
                        max_chars=CHUNK_MAX_CHARS,
                        overlap_chars=CHUNK_OVERLAP_CHARS,
                        document_title=document_title,
                    )
                    if chunks:
                        await loop.run_in_executor(
                            None,
                            self.store.ingest_document,
                            attachment.item_key,
                            item_id,
                            chunks,
                            {
                                "content_type": attachment.content_type,
                                "parent_item_key": item_key,
                                "parent_item_id": item_id,
                                "has_notes": bool(child_notes),
                            },
                        )
                        self.processor.update_indexed_at(attachment.item_key)
                        item_processed = True

                        try:
                            from zocrates.rag.graph_engine import evaluate_and_persist_edges

                            await loop.run_in_executor(
                                None,
                                evaluate_and_persist_edges,
                                attachment.item_key,
                                self,
                                True,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Edge evaluation failed for %s: %s",
                                attachment.item_key,
                                exc,
                            )

        # 2. Ingest each attached child note under its own note_key
        notes_processed = False
        for n in child_notes:
            # Materialize the OKF bundle first (idempotent, non-LLM, <5ms)
            # so notes are accessible via get_document_toc/read_document.
            bundle_dir = self.processor._cache_dir / n.note_key
            if force or not (bundle_dir / "document.md").exists():
                try:
                    materialize_note_bundle(n, self)
                except Exception as exc:
                    logger.warning(
                        "Note bundle materialization failed for %s: %s",
                        n.note_key,
                        exc,
                    )

            if not force and self.store.is_ingested(n.note_key):
                continue
            n_text = convert_note_html_to_markdown(n.note_html)
            if not n_text.strip():
                continue
            n_title = n.title.strip() if n.title.strip() else f"Note on {document_title}"
            n_chunks = chunk_markdown(
                n_text,
                max_chars=CHUNK_MAX_CHARS,
                overlap_chars=CHUNK_OVERLAP_CHARS,
                document_title=n_title,
            )
            if n_chunks:
                await loop.run_in_executor(
                    None,
                    self.store.ingest_document,
                    n.note_key,
                    n.note_id,
                    n_chunks,
                    {"content_type": "text/note", "is_note": True, "parent_item_key": item_key},
                )
                notes_processed = True

                try:
                    from zocrates.rag.graph_engine import evaluate_and_persist_edges

                    await loop.run_in_executor(
                        None,
                        evaluate_and_persist_edges,
                        n.note_key,
                        self,
                        True,
                    )
                except Exception as exc:
                    logger.warning(
                        "Edge evaluation failed for child note %s: %s",
                        n.note_key,
                        exc,
                    )

        if item_processed or notes_processed:
            gc.collect()
            return "processed"
        return "skipped"

    async def _ingest_standalone_note(
        self, note: ZoteroNote, force: bool = False
    ) -> str:
        """Convert and index a standalone Zotero note."""
        # Materialize the OKF bundle first (idempotent, non-LLM, <5ms)
        # so standalone notes are accessible via get_document_toc/read_document.
        bundle_dir = self.processor._cache_dir / note.note_key
        if force or not (bundle_dir / "document.md").exists():
            try:
                materialize_note_bundle(note, self)
            except Exception as exc:
                logger.warning(
                    "Note bundle materialization failed for %s: %s",
                    note.note_key,
                    exc,
                )

        if not force and self.store.is_ingested(note.note_key):
            return "skipped"

        note_text = convert_note_html_to_markdown(note.note_html)
        if not note_text.strip():
            return "skipped"

        note_title = note.title.strip() if note.title.strip() else "Standalone Note"
        chunks = chunk_markdown(
            note_text,
            max_chars=CHUNK_MAX_CHARS,
            overlap_chars=CHUNK_OVERLAP_CHARS,
            document_title=note_title,
        )
        if not chunks:
            return "skipped"

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self.store.ingest_document,
            note.note_key,
            note.note_id,
            chunks,
            {"content_type": "text/note", "is_note": True, "is_standalone": True},
        )

        try:
            from zocrates.rag.graph_engine import evaluate_and_persist_edges

            await loop.run_in_executor(
                None,
                evaluate_and_persist_edges,
                note.note_key,
                self,
                True,
            )
        except Exception as exc:
            logger.warning(
                "Edge evaluation failed for standalone note %s: %s",
                note.note_key,
                exc,
            )

        gc.collect()

        return "processed"

    async def _ingest_standalone_attachment(
        self, attachment: ZoteroAttachment, force: bool = False
    ) -> str:
        """Convert and index a standalone file attachment (no parent item)."""
        parent_item = None
        if attachment.parent_item_id:
            parent_item = self.db.get_item(attachment.parent_item_id)
        elif attachment.parent_item_key:
            parent_item = self.db.get_item_by_key(attachment.parent_item_key)
        p_abstract = parent_item.abstract if parent_item else None

        if not force and self.store.is_ingested(attachment.item_key):
            bundle_dir = self.processor._cache_dir / attachment.item_key
            if bundle_dir.exists() and (bundle_dir / "document.md").exists():
                if not (bundle_dir / "toc.json").exists() or not (bundle_dir / "toc.md").exists():
                    try:
                        from zocrates.rag.bundle_catalog import generate_item_index_md
                        from zocrates.rag.toc_generator import generate_toc, save_toc_files

                        doc_text = (bundle_dir / "document.md").read_text(encoding="utf-8")
                        meta = self.processor.get_metadata(attachment.item_key)
                        abs_text = (meta.get("abstract") if meta else None) or p_abstract
                        abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
                        title = (parent_item.title if parent_item and parent_item.title else None) or (
                            attachment.path.stem.replace("_", " ").strip() if attachment.path else "Standalone Document"
                        )
                        t_json, t_md = generate_toc(
                            doc_text,
                            title=title,
                            abstract=abs_text,
                            abstract_source=abs_src,
                            item_key=attachment.item_key,
                            parent_item_key=attachment.parent_item_key,
                        )
                        save_toc_files(bundle_dir, t_json, t_md)

                        # Persist abstract to conversion_meta if missing or backfilled
                        if abs_text and meta and (not meta.get("abstract") or not meta.get("abstract_source")):
                            self.processor.update_abstract(attachment.item_key, abs_text, abs_src)

                        # Refresh index.md catalog
                        conv_id = meta.get("converter_id", "markitdown") if meta else "markitdown"
                        conv_ver = meta.get("converter_version", "1.0") if meta else "1.0"
                        generate_item_index_md(
                            item_dir=bundle_dir,
                            item_key=attachment.item_key,
                            title=title,
                            converter_id=conv_id,
                            converter_version=conv_ver,
                        )
                    except Exception as exc:
                        logger.warning("Lazy ToC generation failed for standalone %s: %s", attachment.item_key, exc)
            return "skipped"

        if not attachment.path or not attachment.path.exists():
            return "skipped"

        title = (parent_item.title if parent_item and parent_item.title else None) or (
            attachment.path.stem.replace("_", " ").strip() or "Standalone Document"
        )

        loop = asyncio.get_event_loop()
        markdown = await loop.run_in_executor(
            None,
            lambda att=attachment, ti=title, ab=p_abstract: (
                self.processor.get_or_convert(
                    att.item_key,
                    att.path,
                    att.content_type,
                    title=ti,
                    parent_item_key=att.parent_item_key,
                    abstract=ab,
                )
            ),
        )
        if not markdown or not markdown.strip():
            return "skipped"

        try:
            from zocrates.rag.vlm_captioner import enrich_document_bundle

            bundle_dir = self.processor._cache_dir / attachment.item_key
            enriched = await loop.run_in_executor(
                None,
                enrich_document_bundle,
                bundle_dir,
                self._zotero_data_dir,
            )
            if enriched:
                markdown = enriched
        except Exception as exc:
            logger.warning(
                "VLM figure enrichment failed for %s: %s; proceeding with base markdown",
                attachment.item_key,
                exc,
            )

        # Update ToC on final post-enrichment markdown
        try:
            from zocrates.rag.toc_generator import generate_toc, save_toc_files

            bundle_dir = self.processor._cache_dir / attachment.item_key
            meta = self.processor.get_metadata(attachment.item_key)
            abs_text = (meta.get("abstract") if meta else None) or p_abstract
            abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
            t_json, t_md = generate_toc(
                markdown,
                title=title,
                abstract=abs_text,
                abstract_source=abs_src,
                item_key=attachment.item_key,
                parent_item_key=attachment.parent_item_key,
            )
            save_toc_files(bundle_dir, t_json, t_md)
        except Exception as exc:
            logger.warning(
                "Post-enrichment ToC generation failed for standalone %s: %s",
                attachment.item_key,
                exc,
            )

        chunks = chunk_markdown(
            markdown,
            max_chars=CHUNK_MAX_CHARS,
            overlap_chars=CHUNK_OVERLAP_CHARS,
            document_title=title,
        )
        if not chunks:
            return "skipped"

        await loop.run_in_executor(
            None,
            self.store.ingest_document,
            attachment.item_key,
            attachment.item_id,
            chunks,
            {
                "content_type": attachment.content_type,
                "is_standalone": True,
                "document_title": title,
            },
        )

        try:
            from zocrates.rag.graph_engine import evaluate_and_persist_edges

            await loop.run_in_executor(
                None,
                evaluate_and_persist_edges,
                attachment.item_key,
                self,
                True,
            )
        except Exception as exc:
            logger.warning(
                "Edge evaluation failed for standalone attachment %s: %s",
                attachment.item_key,
                exc,
            )

        return "processed"

    async def sync_item(
        self,
        item_id_or_key: int | str | None = None,
        item_key: str | None = None,
        document_title: str = "",
        force: bool = False,
    ) -> str:
        """Sync a single item, note, or attachment by ID or key."""
        target_key: str | None = item_key
        target_id: int | None = None

        if isinstance(item_id_or_key, str):
            target_key = item_id_or_key
        elif isinstance(item_id_or_key, int):
            target_id = item_id_or_key

        if target_id is None and target_key is not None:
            item = self.db.get_item_by_key(target_key)
            if item:
                return await self._ingest_item(item.item_id, item.item_key, item.title, force=force)
            att = self.db.get_attachment_by_key(target_key)
            if att:
                return await self._ingest_standalone_attachment(att, force=force)
            note = self.db.get_note_by_key(target_key)
            if note:
                return await self._ingest_standalone_note(note, force=force)
            return "skipped"

        if target_id is not None:
            item = self.db.get_item(target_id)
            if item:
                return await self._ingest_item(item.item_id, item.item_key, item.title, force=force)
            att = self.db.get_attachment_by_id(target_id)
            if att:
                return await self._ingest_standalone_attachment(att, force=force)
            note = self.db.get_note_by_id(target_id)
            if note:
                return await self._ingest_standalone_note(note, force=force)

        return "skipped"

    def close(self) -> None:
        """Tear down resources held by this syncer instance."""
        logger.info("Closing LibrarySyncer and releasing resources.")
        try:
            if hasattr(self, 'store') and hasattr(self.store, 'close'):
                self.store.close()
            elif hasattr(self, 'store') and hasattr(self.store, '_client'):
                # PersistentClient doesn't require explicit close, but we clear references
                del self.store._client
        except Exception as exc:
            logger.warning("Error during LibrarySyncer teardown: %s", exc)
