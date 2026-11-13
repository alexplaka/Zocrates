"""Pure Python business logic for Zocrates core retrieval and reading toolkit.

This module provides the core implementation of:
- `search_corpus_core`: Hybrid semantic + keyword search with compound pre-filtering
- `get_document_toc_core`: Agent-readable Table of Contents retrieval
- `read_document_core`: Granular, foolproof section/figure/table slicing with Range-Aware Pre-Exclusion
- `get_related_items_core`: Related paper discovery from the literature knowledge graph

Zero framework dependencies (no ADK or MCP imports).
"""

import contextlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from zocrates.rag.service_manager import get_syncer
from zocrates.rag.bundle_catalog import RELATION_TYPE_PRECEDENCE
from zocrates.agents.common.config import MAX_UNIQUE_CACHED_TOCS

logger = logging.getLogger(__name__)

WORDS_PER_PAGE = 3000
MULTI_ASSET_WORD_CAP = 6000
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_item_key(item_key: str) -> bool:
    """Validate item key to prevent path traversal."""
    return bool(item_key and _SAFE_KEY_RE.match(item_key))


def search_corpus_core(
    query: str,
    item_key: str | list[str] | None = None,
    chunk_type: str | list[str] | None = None,
    limit: int = 10,
    exclude_chunk_ids: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """Hybrid (dense vector + FTS5 BM25) search over indexed library chunks with compound pre-filtering.

    Args:
        query: The search query string.
        item_key: Single item key or list of item keys to filter by. Defaults to searching all items.
        chunk_type: Chunk type filter ('frontmatter', 'figure', 'table', 'equation', 'code', 'text').
        limit: Maximum number of results to return.
        exclude_chunk_ids: Set or list of chunk IDs to exclude from search results.

    Returns:
        Dictionary with 'status' and list of clean agent 'results'.
    """
    if not query or not query.strip():
        return {"status": "success", "results": []}

    try:
        syncer = get_syncer()

        # Normalize item_key filter
        norm_item_keys: list[str] | None = None
        if isinstance(item_key, str):
            if item_key.strip().lower() in ("all", "*", "library", "entire_library"):
                norm_item_keys = None
            elif _validate_item_key(item_key):
                norm_item_keys = [item_key]
        elif isinstance(item_key, list):
            norm_item_keys = [
                k
                for k in item_key
                if isinstance(k, str)
                and k.strip().lower() not in ("all", "*", "library", "entire_library")
                and _validate_item_key(k)
            ]
            if not norm_item_keys:
                norm_item_keys = None

        # Normalize chunk_type filter
        norm_chunk_types: list[str] | None = None
        if isinstance(chunk_type, str):
            ct = chunk_type.strip().lower()
            if ct in ("none", "null", "all", "*", ""):
                norm_chunk_types = None
            else:
                norm_chunk_types = [ct]
        elif isinstance(chunk_type, list):
            filtered = [
                t.strip().lower()
                for t in chunk_type
                if isinstance(t, str) and t.strip().lower() not in ("none", "null", "all", "*", "")
            ]
            norm_chunk_types = filtered if filtered else None

        # Normalize exclude_chunk_ids
        norm_exclude: set[str] | None = None
        if exclude_chunk_ids:
            norm_exclude = set(exclude_chunk_ids)

        search_results = syncer.store.search(
            query=query,
            final_k=limit,
            item_key_filter=norm_item_keys,
            chunk_type_filter=norm_chunk_types,
            exclude_chunk_ids=norm_exclude,
        )

        cand_keys = {r.item_key for r in search_results if r.item_key}
        if syncer.db and cand_keys:
            active_keys = syncer.db.get_active_item_keys(list(cand_keys))
            if isinstance(active_keys, (set, list, tuple, frozenset)):
                search_results = [r for r in search_results if r.item_key in active_keys]

        # Cache item titles and metadata
        title_cache: dict[str, str] = {}
        authors_cache: dict[str, list[str]] = {}
        year_cache: dict[str, str] = {}
        serialized: list[dict[str, Any]] = []

        for r in search_results:
            if r.item_key not in title_cache:
                item = None
                if syncer.db and r.item_id:
                    cand = syncer.db.get_item(r.item_id)
                    cand_key = (
                        getattr(cand, "item_key", getattr(cand, "key", None))
                        if cand
                        else None
                    )
                    if cand and (
                        cand_key == r.item_key or not isinstance(cand_key, str)
                    ):
                        item = cand
                if not item and syncer.db and r.item_key:
                    item = syncer.db.get_item_by_key(r.item_key)

                parent_key = getattr(r, "parent_item_key", None)
                if not parent_key and syncer.processor:
                    meta = syncer.processor.get_metadata(r.item_key)
                    if isinstance(meta, dict):
                        parent_key = meta.get("parent_item_key")

                parent_item = None
                if parent_key and syncer.db:
                    parent_item = syncer.db.get_item_by_key(parent_key)

                raw_title = item.title if item else ""
                if (not raw_title or raw_title.lower() in ("snapshot", "full text", "attachment")) and parent_item:
                    raw_title = parent_item.title
                if not raw_title and syncer.processor:
                    meta = syncer.processor.get_metadata(r.item_key)
                    if isinstance(meta, dict) and meta.get("title"):
                        raw_title = meta["title"]
                title_cache[r.item_key] = raw_title or "Untitled Document"

                extracted_authors = getattr(item, "creators", []) or [] if item else []
                if not extracted_authors and parent_item:
                    extracted_authors = getattr(parent_item, "creators", []) or []
                if not isinstance(extracted_authors, (list, tuple)):
                    extracted_authors = []
                elif not extracted_authors and getattr(r, "creators", None):
                    extracted_authors = list(r.creators)

                # Fallback to document.md frontmatter for authors
                if not extracted_authors:
                    bundle_res = _resolve_bundle_dir(r.item_key)
                    if bundle_res:
                        doc_md = bundle_res[0] / "document.md"
                        if doc_md.exists():
                            try:
                                import yaml
                                lines = doc_md.read_text(encoding="utf-8").split("---", 2)
                                if len(lines) >= 3:
                                    fm = yaml.safe_load(lines[1])
                                    if isinstance(fm, dict) and fm.get("authors"):
                                        extracted_authors = fm["authors"]
                            except Exception:
                                pass
                authors_cache[r.item_key] = list(extracted_authors)

                extracted_year = getattr(item, "year", "") or "" if item else ""
                if not extracted_year and parent_item:
                    extracted_year = getattr(parent_item, "year", "") or ""
                if not isinstance(extracted_year, str):
                    extracted_year = ""
                elif not extracted_year and getattr(r, "year", None):
                    extracted_year = str(r.year)

                # Fallback to document.md frontmatter for year
                if not extracted_year:
                    bundle_res = _resolve_bundle_dir(r.item_key)
                    if bundle_res:
                        doc_md = bundle_res[0] / "document.md"
                        if doc_md.exists():
                            try:
                                import yaml
                                lines = doc_md.read_text(encoding="utf-8").split("---", 2)
                                if len(lines) >= 3:
                                    fm = yaml.safe_load(lines[1])
                                    if isinstance(fm, dict):
                                        if fm.get("year"):
                                            extracted_year = str(fm["year"])
                                        elif fm.get("date"):
                                            m = re.search(r"\b(19\d\d|20\d\d)\b", str(fm["date"]))
                                            if m:
                                                extracted_year = m.group(1)
                            except Exception:
                                pass
                year_cache[r.item_key] = str(extracted_year) if extracted_year else ""

            heading_str = " > ".join(r.heading_path) if r.heading_path else ""
            word_count = len(r.chunk_text.split()) if r.chunk_text else 0

            serialized.append(
                {
                    "chunk_id": r.chunk_id or f"{r.item_key}::{r.chunk_index}",
                    "item_key": r.item_key,
                    "title": title_cache[r.item_key],
                    "authors": authors_cache[r.item_key],
                    "year": year_cache[r.item_key],
                    "chunk_type": r.chunk_type,
                    "heading": heading_str,
                    "text": r.chunk_text,
                    "word_count": word_count,
                    "image_path": r.image_path,
                    "cosine_similarity": r.raw_semantic_score,
                    "rrf_score": r.score,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                }
            )

        return {"status": "success", "results": serialized}
    except Exception as exc:
        logger.error("Failed in search_corpus_core: %s", exc)
        return {"status": "error", "message": str(exc)}


# Content-type priority for attachment resolution (lower number = higher priority).
_CONTENT_TYPE_PRIORITY: dict[str, int] = {
    "application/pdf": 0,
    "text/html": 1,
    "text/markdown": 2,
    "text/plain": 3,
}


def _resolve_bundle_dir(item_key: str) -> tuple[Path, str, str | None] | None:
    """Resolve bundle directory, active key, and optional disambiguation notice.

    Three-step resolution:
    1. Note on-demand: ``item_key`` matches a Zotero note → materialise its
       bundle first so staleness is always validated via the SHA-256 fast-path.
    2. Direct check: ``cache_dir / item_key`` has ``document.md`` or ``toc.md``.
    3. Parent key fallback: look up converted child attachments (PDF-first sort);
       if none, fall back to materialising the first child note.

    Args:
        item_key: Zotero item key.

    Returns:
        ``(bundle_dir, resolved_key, notice)`` where ``notice`` is a non-empty
        string when the parent item has multiple artifacts and the caller should
        be informed about alternatives, or ``None`` if everything is unambiguous.
        Returns ``None`` altogether when no bundle can be resolved.
    """
    if not _validate_item_key(item_key):
        return None

    syncer = get_syncer()
    cache_dir = syncer.processor._cache_dir
    bundle_dir = cache_dir / item_key

    # Liveness check: If syncer.db is present and not syncer.db.is_item_active(item_key):
    #   Do NOT return bundle or materialize! Return None immediately so parent-deleted notes
    #   and directly-deleted items are not materialized or queried.
    if syncer.db and not syncer.db.is_item_active(item_key):
        return None

    # Step 1: Check whether item_key is a note first, so staleness is always
    # validated via materialize_note_bundle's SHA-256 fast-path even when a
    # bundle directory already exists on disk. On failure (or a non-note key)
    # execution falls through to the direct bundle check below.
    try:
        note = syncer.db.get_note_by_key(item_key)
    except Exception:
        note = None
    if note is not None and getattr(note, "note_key", None):
        try:
            from zocrates.rag.document_processor import materialize_note_bundle  # local import avoids cycles
            note_dir, resolved_note_key = materialize_note_bundle(note, syncer, cache_dir)
            return note_dir, resolved_note_key, None
        except Exception as exc:
            logger.error("Failed to materialise note bundle for %s: %s", item_key, exc)

    # Step 2: Direct bundle check (attachment or already-resolved key).
    # If this is a converted attachment or note bundle (content_type != 'text/zotero-metadata'),
    # return it immediately. Only metadata-only bundles (text/zotero-metadata) fall through
    # to Step 3 so any newly converted attachments take priority and freshness is validated.
    if (bundle_dir / "document.md").exists() or (bundle_dir / "toc.md").exists():
        if bundle_dir.resolve().is_relative_to(cache_dir.resolve()):
            meta = syncer.processor.get_metadata(item_key) if hasattr(syncer, "processor") else None
            if not meta or meta.get("content_type") != "text/zotero-metadata":
                return bundle_dir, item_key, None

    # Step 3: Parent key fallback.
    item = syncer.db.get_item_by_key(item_key)
    if not item:
        return None

    attachments = syncer.db.get_attachments(item.item_id)
    converted_atts = [
        att for att in attachments
        if (cache_dir / att.item_key / "document.md").exists()
        and (cache_dir / att.item_key).resolve().is_relative_to(cache_dir.resolve())
    ]
    converted_atts.sort(key=lambda a: _CONTENT_TYPE_PRIORITY.get(a.content_type, 99))

    # Also gather child notes for disambiguation notice construction.
    try:
        notes = syncer.db.get_notes_for_item(item.item_id)
    except Exception:
        notes = []

    if converted_atts:
        primary = converted_atts[0]
        primary_dir = cache_dir / primary.item_key
        primary_name = primary.title or (primary.path.name if primary.path else primary.item_key)

        # Build notice when siblings exist.
        other_parts: list[str] = []
        for att in converted_atts[1:]:
            att_name = att.title or (att.path.name if att.path else att.item_key)
            other_parts.append(f"[Attachment Key: {att.item_key}] {att_name!r} [{att.content_type}]")
        for n in notes:
            n_name = n.title or "Note"
            other_parts.append(f"[Note Key: {n.note_key}] {n_name!r} [text/note]")

        notice: str | None = None
        if other_parts:
            notice = (
                f"Loaded primary attachment {primary_name!r}. "
                f"This item has additional artifacts: {', '.join(other_parts)}. "
                f"Call get_document_toc with a specific key to inspect them."
            )

        return primary_dir, primary.item_key, notice

    # No converted attachments: materialize metadata bundle if attachment-free
    valid_attachments = [a for a in attachments if a.path and a.path.exists()]
    if item.item_type not in ("attachment", "note", "annotation") and not valid_attachments:
        try:
            from zocrates.rag.document_processor import materialize_item_metadata_bundle
            item_bundle_dir, resolved_item_key = materialize_item_metadata_bundle(item, syncer, cache_dir)
            notice = None
            if notes:
                note_parts = [
                    f"[Note Key: {n.note_key}] {(n.title or 'Note')!r} [text/note]"
                    for n in notes
                ]
                notice = (
                    f"Loaded bibliographic metadata for {(item.title or item_key)!r}. "
                    f"This item has child notes: {', '.join(note_parts)}. "
                    f"Call get_document_toc with a specific key to inspect them."
                )
            return item_bundle_dir, resolved_item_key, notice
        except Exception as exc:
            logger.error("Failed to materialize metadata bundle for %s: %s", item_key, exc)

    # No converted attachments: materialise the first note as last resort.
    if notes:
        try:
            from zocrates.rag.document_processor import materialize_note_bundle
            note_dir, resolved_note_key = materialize_note_bundle(notes[0], syncer, cache_dir)

            fallback_notice: str | None = None
            if len(notes) > 1:
                other_notes = [
                    f"[Note Key: {n.note_key}] {(n.title or 'Note')!r} [text/note]"
                    for n in notes[1:]
                ]
                fallback_notice = (
                    f"Loaded note {(notes[0].title or 'Note')!r}. "
                    f"This item has additional notes: {', '.join(other_notes)}. "
                    f"Call get_document_toc with a specific key to inspect them."
                )

            return note_dir, notes[0].note_key, fallback_notice
        except Exception as exc:
            logger.error(
                "Failed to materialise note bundle for %s (last-resort): %s",
                notes[0].note_key,
                exc,
            )

    return None


def get_document_toc_core(item_key: str) -> dict[str, Any]:
    """Retrieve the human- and agent-readable Table of Contents, Title, Abstract, Figures, and Tables.

    Args:
        item_key: Unique Zotero item key.

    Returns:
        Dictionary with 'status', 'item_key', and 'toc_content'.
    """
    if not _validate_item_key(item_key):
        return {"status": "error", "message": f"Invalid item key: {item_key}"}

    try:
        resolved = _resolve_bundle_dir(item_key)
        if not resolved:
            return {
                "status": "error",
                "message": f"Document bundle for {item_key} not found or not yet converted.",
            }

        bundle_dir, resolved_key, res_notice = resolved
        toc_md_path = bundle_dir / "toc.md"
        if toc_md_path.exists():
            result: dict[str, Any] = {
                "status": "success",
                "item_key": resolved_key,
                "toc_content": toc_md_path.read_text(encoding="utf-8"),
            }
            if res_notice:
                result["notice"] = res_notice
            return result

        # Lazy generation fallback if document.md exists but toc.md was not yet generated
        doc_md_path = bundle_dir / "document.md"
        if doc_md_path.exists():
            syncer = get_syncer()
            doc_text = doc_md_path.read_text(encoding="utf-8")
            meta = syncer.processor.get_metadata(resolved_key)
            title = meta.get("title") if meta else None
            parent_item_key = meta.get("parent_item_key") if meta else None
            from zocrates.rag.toc_generator import generate_toc, save_toc_files

            toc_json, toc_md = generate_toc(
                doc_text,
                title=title,
                item_key=resolved_key,
                parent_item_key=parent_item_key,
            )
            save_toc_files(bundle_dir, toc_json, toc_md)
            result = {
                "status": "success",
                "item_key": resolved_key,
                "toc_content": toc_md,
            }
            if res_notice:
                result["notice"] = res_notice
            return result

        return {
            "status": "error",
            "message": f"Document bundle for {item_key} has no document.md or toc.md.",
        }
    except Exception as exc:
        logger.error("Failed in get_document_toc_core for %s: %s", item_key, exc)
        return {"status": "error", "message": str(exc)}


def normalize_asset_ids(
    asset_id: str | list[str] | None,
) -> list[str] | None:
    """Normalize raw asset_id input from LLM into a canonical list of asset ID strings, or None.

    Handles:
      - None -> None (full document)
      - JSON array string: '["s1", "s2"]' -> ["s1", "s2"]
      - Bracketed list string: '[s3, s4, s5]' -> ["s3", "s4", "s5"]
      - Single bracketed string: '[s1]' -> ["s1"]
      - Comma-separated string: 's1, s2, s3' -> ["s1", "s2", "s3"]
      - Quoted string: '"s1"' -> ["s1"]
      - Python list / tuple / set of strings: ["s1", "s2"] -> ["s1", "s2"]
      - Preserves order and deduplicates identical IDs.
    """
    if asset_id is None:
        return None

    def _strip_wrapping(s: Any) -> str:
        """Strip enclosing brackets, braces, and quotes from a string token.

        Args:
            s: Value or token to strip wrapping delimiters from.

        Returns:
            Unwrapped string.
        """
        t = str(s).strip()
        changed = True
        while changed and len(t) >= 2:
            changed = False
            for open_b, close_b in [("[", "]"), ("{", "}"), ("(", ")"), ('"', '"'), ("'", "'")]:
                if t.startswith(open_b) and t.endswith(close_b):
                    t = t[len(open_b) : -len(close_b)].strip()
                    changed = True
                    break
        return t

    tokens: list[str] = []

    if isinstance(asset_id, str):
        raw_str = asset_id.strip()
        if not raw_str:
            return None

        # 1. Try parsing as JSON array
        if raw_str.startswith("[") and raw_str.endswith("]"):
            try:
                parsed = json.loads(raw_str)
                if isinstance(parsed, list):
                    tokens = [str(item) for item in parsed if item is not None]
            except Exception:
                pass

        # 2. If not parsed as JSON, strip outer brackets/braces/parens (not quotes spanning commas)
        if not tokens:
            unwrapped = raw_str
            while len(unwrapped) >= 2 and (
                (unwrapped[0] == "[" and unwrapped[-1] == "]")
                or (unwrapped[0] == "{" and unwrapped[-1] == "}")
                or (unwrapped[0] == "(" and unwrapped[-1] == ")")
            ):
                unwrapped = unwrapped[1:-1].strip()

            if "," in unwrapped:
                tokens = [part for part in unwrapped.split(",") if part.strip()]
            elif unwrapped:
                tokens = [unwrapped]

    elif isinstance(asset_id, (list, tuple, set)):
        tokens = [str(item) for item in asset_id if item is not None]
    else:
        tokens = [str(asset_id)]

    # Clean individual tokens and deduplicate preserving order
    result: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        c = _strip_wrapping(tok)
        if c and c not in seen:
            seen.add(c)
            result.append(c)

    return result if result else None


def read_document_core(
    item_key: str,
    asset_id: str | list[str] | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Read full document Markdown or targeted sections, figures, tables, or abstract from the OKF bundle.

    Accepts exact asset IDs from toc.md ('s0', 's1', 'f1', 't1'), 'abstract' (or 'summary') to read the
    document's abstract, a list of IDs, or fuzzy heading names.
    Supports Range-Aware Pre-Exclusion by returning all intersecting vector chunk IDs.

    Args:
        item_key: Unique Zotero item key.
        asset_id: Asset ID, list of asset IDs, or fuzzy heading string. If None, reads full document paginated.
        page: 1-indexed page number if content exceeds WORDS_PER_PAGE.

    Returns:
        Dictionary with 'status', 'item_key', 'assets', and 'intersecting_chunk_ids'.
    """
    if not _validate_item_key(item_key):
        return {"status": "error", "message": f"Invalid item key: {item_key}"}

    try:
        resolved = _resolve_bundle_dir(item_key)
        if not resolved:
            return {
                "status": "error",
                "message": f"Document bundle for {item_key} not found or not yet converted.",
            }

        bundle_dir, resolved_key, read_notice = resolved
        doc_md_path = bundle_dir / "document.md"
        if not doc_md_path.exists():
            return {
                "status": "error",
                "message": f"Document markdown for {item_key} not found.",
            }

        syncer = get_syncer()

        # Ensure toc.json exists
        toc_json_path = bundle_dir / "toc.json"
        if not toc_json_path.exists():
            doc_text = doc_md_path.read_text(encoding="utf-8")
            meta = syncer.processor.get_metadata(resolved_key)
            title = meta.get("title") if meta else None
            parent_item_key = meta.get("parent_item_key") if meta else None
            from zocrates.rag.toc_generator import generate_toc, save_toc_files

            toc_json_data, toc_md = generate_toc(
                doc_text,
                title=title,
                item_key=resolved_key,
                parent_item_key=parent_item_key,
            )
            save_toc_files(bundle_dir, toc_json_data, toc_md)
        else:
            toc_json_data = json.loads(toc_json_path.read_text(encoding="utf-8"))

        doc_title = (
            toc_json_data.get("title")
            or (title if "title" in locals() and title else "")
            or ""
        )
        doc_authors = toc_json_data.get("authors")
        doc_year = toc_json_data.get("year")

        if (doc_authors is None or doc_year is None or not doc_title or doc_title.lower() in ("snapshot", "full text", "attachment")) and hasattr(syncer, "processor") and hasattr(syncer.processor, "get_metadata"):
            try:
                meta = syncer.processor.get_metadata(resolved_key)
                if isinstance(meta, dict):
                    if not doc_title and meta.get("title"):
                        doc_title = meta.get("title")
                    if doc_authors is None and meta.get("authors"):
                        doc_authors = meta.get("authors")
                    if doc_year is None and meta.get("year"):
                        doc_year = meta.get("year")
                    parent_key = meta.get("parent_item_key")
                    if parent_key and syncer.db:
                        parent_item = syncer.db.get_item_by_key(parent_key)
                        if parent_item:
                            if not doc_title or doc_title.lower() in ("snapshot", "full text", "attachment"):
                                doc_title = parent_item.title
                            if doc_authors is None and parent_item.creators:
                                doc_authors = parent_item.creators
                            if doc_year is None and parent_item.year:
                                doc_year = parent_item.year
            except Exception:
                pass

        if doc_authors is None or doc_year is None or not doc_title:
            try:
                doc_text_head = doc_md_path.read_text(encoding="utf-8")
                if doc_text_head.startswith("---"):
                    import yaml
                    parts = doc_text_head.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1])
                        if isinstance(fm, dict):
                            if (not doc_title or doc_title.lower() in ("snapshot", "full text", "attachment")) and fm.get("title"):
                                doc_title = str(fm["title"]).strip()
                            if doc_authors is None and fm.get("authors"):
                                doc_authors = fm["authors"]
                            if doc_year is None and fm.get("year"):
                                doc_year = str(fm["year"])
                            elif doc_year is None and fm.get("date"):
                                m = re.search(r"\b(19\d\d|20\d\d)\b", str(fm["date"]))
                                if m:
                                    doc_year = m.group(1)
            except Exception:
                pass

        doc_text = doc_md_path.read_text(encoding="utf-8")
        doc_lines = doc_text.splitlines(keepends=True)

        def slice_lines(start_line: int, end_line: int) -> str:
            """Extract a 1-based inclusive line range from document lines.

            Args:
                start_line: 1-based start line index.
                end_line: 1-based end line index.

            Returns:
                Joined line content for the specified range.
            """
            s_idx = max(0, start_line - 1)
            e_idx = max(0, end_line)
            return "".join(doc_lines[s_idx:e_idx])

        # Precompute hierarchical breadcrumb headings for section assets
        def _is_fm_or_title(h_text: str) -> bool:
            clean_h = h_text.strip().lower()
            if not clean_h or clean_h in ("frontmatter", "title", "document body"):
                return True
            if re.match(r"^(?:figure|table)\s+\d", clean_h, re.IGNORECASE):
                return True
            if doc_title:
                norm_h = re.sub(r"[^\w\s]", "", clean_h).replace(" ", "")
                norm_t = re.sub(r"[^\w\s]", "", doc_title.lower()).replace(" ", "")
                if norm_h and norm_h == norm_t:
                    return True
            return False

        sec_breadcrumb_headings: dict[int, str] = {}
        heading_stack: list[dict[str, Any]] = []

        for sec in toc_json_data.get("sections", []):
            s_id = sec.get("id", "")
            raw_h = sec.get("heading", "")
            lvl = int(sec.get("level") or 1)

            if sec.get("level") in (0, "0") or s_id == "s0" or _is_fm_or_title(raw_h):
                sec_breadcrumb_headings[id(sec)] = raw_h
                continue

            while heading_stack and heading_stack[-1]["level"] >= lvl:
                heading_stack.pop()

            if " > " in raw_h:
                computed_h = raw_h
                leaf_h = raw_h.split(" > ")[-1].strip()
            else:
                parents = [item["heading"] for item in heading_stack]
                if parents:
                    computed_h = f"{' > '.join(parents)} > {raw_h}"
                else:
                    computed_h = raw_h
                leaf_h = raw_h

            sec_breadcrumb_headings[id(sec)] = computed_h
            heading_stack.append({"level": lvl, "heading": leaf_h})

        def _get_sec_heading(s_dict: dict[str, Any]) -> str:
            return sec_breadcrumb_headings.get(id(s_dict), s_dict.get("heading", ""))

        # Case 1: Whole document paginated mode (asset_id is None)
        if asset_id is None:
            if not doc_lines:
                doc_lines = [""]

            # Partition lines into pages of up to WORDS_PER_PAGE words
            pages: list[tuple[int, int]] = []
            cur_start_idx = 0
            cur_words = 0
            for idx, line in enumerate(doc_lines):
                lw = len(line.split())
                if cur_words > 0 and cur_words + lw > WORDS_PER_PAGE:
                    pages.append((cur_start_idx + 1, idx))
                    cur_start_idx = idx
                    cur_words = lw
                else:
                    cur_words += lw

            pages.append((cur_start_idx + 1, len(doc_lines)))
            total_pages = len(pages)
            req_page = max(1, min(page or 1, total_pages))
            page_start_line, page_end_line = pages[req_page - 1]
            content = slice_lines(page_start_line, page_end_line)
            page_words = content.split()
            has_more = req_page < total_pages

            inter_chunk_ids = syncer.store.get_intersecting_chunk_ids(
                resolved_key, page_start_line, page_end_line
            )
            asset_obj = {
                "asset_id": "full",
                "asset_type": "document",
                "heading": "Full Document",
                "content": content,
                "word_count": len(page_words),
                "path": None,
                "structured_content": None,
                "page": req_page,
                "total_pages": total_pages,
                "has_more": has_more,
            }
            full_doc_result: dict[str, Any] = {
                "status": "success",
                "item_key": resolved_key,
                "title": doc_title,
                "assets": [asset_obj],
                "intersecting_chunk_ids": inter_chunk_ids,
            }
            if doc_authors is not None:
                full_doc_result["authors"] = doc_authors
            if doc_year is not None:
                full_doc_result["year"] = doc_year
            if read_notice:
                full_doc_result["notice"] = read_notice
            return full_doc_result

        # Helper to resolve a single asset ID or fuzzy heading
        def _resolve_single_asset(aid: str, req_page: int | None = None) -> tuple[dict[str, Any], list[str]]:
            """Resolve a single asset ID or section heading into an asset payload and intersecting chunks.

            Args:
                aid: Asset ID (e.g. 's1', 'f2', 't1') or section heading title.
                req_page: Optional 1-based page number for paginated section reading.

            Returns:
                A tuple containing the asset dictionary and a list of intersecting chunk IDs.

            Raises:
                KeyError: If the requested asset ID or heading is not found in the TOC manifest.
            """
            clean_aid = aid.strip()
            clean_lower = clean_aid.lower()

            # 0. Virtual Abstract / Summary match ("abstract", "summary")
            if clean_lower in ("abstract", "summary"):
                # Delegate to an explicit section if present
                for sec in toc_json_data.get("sections", []):
                    if sec.get("heading", "").lower().strip() in ("abstract", "summary"):
                        start_l = sec.get("start_line", 1)
                        end_l = sec.get("end_line", start_l)
                        raw_content = slice_lines(start_l, end_l)
                        inter_ids = syncer.store.get_intersecting_chunk_ids(resolved_key, start_l, end_l)
                        words = raw_content.split()
                        total_w = len(words)
                        tot_pages = max(1, (total_w + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
                        p = max(1, min(req_page or 1, tot_pages))
                        if tot_pages > 1:
                            p_words = words[(p - 1) * WORDS_PER_PAGE : p * WORDS_PER_PAGE]
                            p_content = " ".join(p_words)
                            has_m = p < tot_pages
                        else:
                            p_content = raw_content
                            p_words = words
                            has_m = False

                        return {
                            "asset_id": sec["id"],
                            "asset_type": "section",
                            "heading": _get_sec_heading(sec),
                            "content": p_content,
                            "word_count": len(p_words),
                            "path": None,
                            "structured_content": None,
                            "page": p,
                            "total_pages": tot_pages,
                            "has_more": has_m,
                        }, inter_ids

                # Fallback to metadata abstract from toc.json
                abstract_text = (toc_json_data.get("abstract") or "").strip()
                abs_src = (toc_json_data.get("abstract_source") or "").strip().lower()
                if abstract_text and abstract_text != "No abstract or summary available." and abs_src != "none":
                    words = abstract_text.split()
                    total_w = len(words)
                    tot_pages = max(1, (total_w + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
                    p = max(1, min(req_page or 1, tot_pages))
                    if tot_pages > 1:
                        p_words = words[(p - 1) * WORDS_PER_PAGE : p * WORDS_PER_PAGE]
                        p_content = " ".join(p_words)
                        has_m = p < tot_pages
                    else:
                        p_content = abstract_text
                        p_words = words
                        has_m = False

                    return {
                        "asset_id": "abstract",
                        "asset_type": "abstract",
                        "heading": "Abstract",
                        "content": p_content,
                        "word_count": len(p_words),
                        "path": None,
                        "structured_content": None,
                        "page": p,
                        "total_pages": tot_pages,
                        "has_more": has_m,
                    }, []

                raise KeyError(f"No abstract or summary available for document {resolved_key}.")

            # 1. Section match (s0, s1, ...)
            for sec in toc_json_data.get("sections", []):
                if sec.get("id") == clean_aid:
                    start_l = sec.get("start_line", 1)
                    end_l = sec.get("end_line", start_l)
                    raw_content = slice_lines(start_l, end_l)
                    inter_ids = syncer.store.get_intersecting_chunk_ids(resolved_key, start_l, end_l)
                    words = raw_content.split()
                    total_w = len(words)
                    tot_pages = max(1, (total_w + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
                    p = max(1, min(req_page or 1, tot_pages))
                    if tot_pages > 1:
                        p_words = words[(p - 1) * WORDS_PER_PAGE : p * WORDS_PER_PAGE]
                        p_content = " ".join(p_words)
                        has_m = p < tot_pages
                    else:
                        p_content = raw_content
                        p_words = words
                        has_m = False

                    return {
                        "asset_id": sec["id"],
                        "asset_type": "section",
                        "heading": _get_sec_heading(sec),
                        "content": p_content,
                        "word_count": len(p_words),
                        "path": None,
                        "structured_content": None,
                        "page": p,
                        "total_pages": tot_pages,
                        "has_more": has_m,
                    }, inter_ids

            # 2. Figure match (f1, f2, ...)
            for fig in toc_json_data.get("figures", []):
                if fig.get("id") == clean_aid:
                    start_l = fig.get("start_line", 1)
                    end_l = fig.get("end_line", start_l)
                    raw_content = slice_lines(start_l, end_l)
                    inter_ids = syncer.store.get_intersecting_chunk_ids(resolved_key, start_l, end_l)
                    return {
                        "asset_id": fig["id"],
                        "asset_type": "figure",
                        "heading": fig.get("caption", "") or "",
                        "content": raw_content,
                        "word_count": len(raw_content.split()),
                        "path": fig.get("path"),
                        "structured_content": None,
                        "page": 1,
                        "total_pages": 1,
                        "has_more": False,
                    }, inter_ids

            # 3. Table match (t1, t2, ...)
            for tab in toc_json_data.get("tables", []):
                if tab.get("id") == clean_aid:
                    start_l = tab.get("start_line", 1)
                    end_l = tab.get("end_line", start_l)
                    raw_content = slice_lines(start_l, end_l)
                    inter_ids = syncer.store.get_intersecting_chunk_ids(resolved_key, start_l, end_l)
                    csv_content = None
                    if tab.get("path"):
                        csv_p = (bundle_dir / tab["path"]).resolve()
                        if csv_p.is_relative_to(bundle_dir.resolve()) and csv_p.exists():
                            csv_content = csv_p.read_text(encoding="utf-8")
                    return {
                        "asset_id": tab["id"],
                        "asset_type": "table",
                        "heading": tab.get("caption", "") or "",
                        "content": raw_content,
                        "word_count": len(raw_content.split()),
                        "path": tab.get("path"),
                        "structured_content": csv_content,
                        "page": 1,
                        "total_pages": 1,
                        "has_more": False,
                    }, inter_ids

            # 4. Fuzzy heading matching fallback
            norm_aid = re.sub(r"[^\w\s]", "", clean_aid).lower().strip()
            best_sec = None
            for sec in toc_json_data.get("sections", []):
                h = sec.get("heading", "")
                norm_h = re.sub(r"[^\w\s]", "", h).lower().strip()
                b_h = _get_sec_heading(sec)
                norm_b = re.sub(r"[^\w\s]", "", b_h).lower().strip()
                if (
                    norm_aid == norm_h
                    or (norm_aid and norm_aid in norm_h)
                    or norm_aid == norm_b
                    or (norm_aid and norm_aid in norm_b)
                ):
                    best_sec = sec
                    break

            if best_sec is not None:
                start_l = best_sec.get("start_line", 1)
                end_l = best_sec.get("end_line", start_l)
                raw_content = slice_lines(start_l, end_l)
                inter_ids = syncer.store.get_intersecting_chunk_ids(resolved_key, start_l, end_l)
                words = raw_content.split()
                total_w = len(words)
                tot_pages = max(1, (total_w + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
                p = max(1, min(req_page or 1, tot_pages))
                if tot_pages > 1:
                    p_words = words[(p - 1) * WORDS_PER_PAGE : p * WORDS_PER_PAGE]
                    p_content = " ".join(p_words)
                    has_m = p < tot_pages
                else:
                    p_content = raw_content
                    p_words = words
                    has_m = False
                return {
                    "asset_id": best_sec["id"],
                    "asset_type": "section",
                    "heading": _get_sec_heading(best_sec),
                    "content": p_content,
                    "word_count": len(p_words),
                    "path": None,
                    "structured_content": None,
                    "page": p,
                    "total_pages": tot_pages,
                    "has_more": has_m,
                }, inter_ids

            raise KeyError(f"Asset ID or heading '{clean_aid}' not found in document bundle.")

        norm_asset_ids = normalize_asset_ids(asset_id)

        # Case 1: Whole document paginated mode (norm_asset_ids is None)
        # Note: if asset_id was None, it was already handled above.
        if norm_asset_ids is None:
            return {
                "status": "error",
                "message": f"Invalid asset_id: '{asset_id}'",
            }

        # Case 2: Single asset string
        if len(norm_asset_ids) == 1:
            aid = norm_asset_ids[0]
            try:
                single_asset, inter_ids = _resolve_single_asset(aid, req_page=page)
                single_result: dict[str, Any] = {
                    "status": "success",
                    "item_key": resolved_key,
                    "title": doc_title,
                    "assets": [single_asset],
                    "intersecting_chunk_ids": inter_ids,
                }
                if doc_authors is not None:
                    single_result["authors"] = doc_authors
                if doc_year is not None:
                    single_result["year"] = doc_year
                if read_notice:
                    single_result["notice"] = read_notice
                return single_result
            except KeyError as err:
                return {"status": "error", "message": str(err)}

        # Case 3: List of asset IDs
        assets: list[dict[str, Any]] = []
        all_inter: list[str] = []
        total_words_accum = 0
        unresolved_ids: list[str] = []

        for aid in norm_asset_ids:
            try:
                asset_obj, inter_ids = _resolve_single_asset(aid, req_page=1)
                all_inter.extend(inter_ids)
                if total_words_accum + asset_obj["word_count"] > MULTI_ASSET_WORD_CAP:
                    remaining = max(0, MULTI_ASSET_WORD_CAP - total_words_accum)
                    if remaining > 0:
                        w_slice = asset_obj["content"].split()[:remaining]
                        asset_obj["content"] = " ".join(w_slice)
                        asset_obj["word_count"] = len(w_slice)
                        asset_obj["has_more"] = True
                        assets.append(asset_obj)
                    break
                else:
                    total_words_accum += asset_obj["word_count"]
                    assets.append(asset_obj)
            except KeyError:
                unresolved_ids.append(aid)

        # Deduplicate intersecting IDs preserving order
        seen: set[str] = set()
        deduped_inter = [cid for cid in all_inter if not (cid in seen or seen.add(cid))]

        status = "success" if assets else "error"
        result_payload: dict[str, Any] = {
            "status": status,
            "item_key": resolved_key,
            "title": doc_title,
            "assets": assets,
            "intersecting_chunk_ids": deduped_inter,
        }
        if doc_authors is not None:
            result_payload["authors"] = doc_authors
        if doc_year is not None:
            result_payload["year"] = doc_year
        if unresolved_ids:
            result_payload["unresolved_asset_ids"] = unresolved_ids
            result_payload["notice"] = (
                f"[Warning: Asset IDs {unresolved_ids} could not be resolved in document {resolved_key}. "
                "Verify asset IDs against get_document_toc output.]"
            )
        elif read_notice:
            result_payload["notice"] = read_notice

        return result_payload
    except Exception as exc:
        logger.error("Failed in read_document_core for %s: %s", item_key, exc)
        return {"status": "error", "message": str(exc)}


def get_related_items_core(
    item_key: str,
    ceiling: int = 25,
) -> dict[str, Any]:
    """Retrieve verified related papers for a given document from the knowledge graph.

    Args:
        item_key: Unique Zotero item or attachment key.
        ceiling: Maximum number of related items to return (default: 25).

    Returns:
        Dictionary with status, item_key, total_count, and list of related items:
        {
            "status": "success",
            "item_key": resolved_key,
            "total_count": len(related_items),
            "related_items": [
                {
                    "item_key": t_key,
                    "title": title,
                    "relation_type": rel_type,
                    "score": score,
                    "parent_item_key": parent_key,
                }, ...
            ],
            # Optional: "notice": disambiguation notice if res_notice present
        }
    """
    if not _validate_item_key(item_key):
        return {"status": "error", "message": f"Invalid item key: {item_key}"}

    try:
        resolved = _resolve_bundle_dir(item_key)
        if not resolved:
            return {
                "status": "error",
                "message": f"Document bundle for {item_key} not found or not yet converted.",
            }

        bundle_dir, resolved_key, res_notice = resolved

        cache_dir = bundle_dir.parent if bundle_dir else None
        if not cache_dir:
            try:
                syncer = get_syncer()
                if syncer and getattr(syncer, "processor", None):
                    cache_dir = getattr(syncer.processor, "_cache_dir", None)
            except Exception:
                pass

        related_items: list[dict[str, Any]] = []

        if cache_dir:
            meta_db_path = cache_dir / "conversion_meta.db"
            if meta_db_path.exists():
                try:
                    with contextlib.closing(sqlite3.connect(str(meta_db_path), timeout=5.0)) as conn:
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA busy_timeout = 5000;")
                        try:
                            rows = conn.execute(
                                """
                                SELECT target_key, score, relation_type
                                FROM item_relations
                                WHERE source_key = ?
                                ORDER BY score DESC, target_key ASC
                                """,
                                (resolved_key,),
                            ).fetchall()
                        except sqlite3.OperationalError:
                            rows = []

                        # Coalesce multiple relation types for the same target_key using strict precedence:
                        # human_curated_and_hybrid_search (1.0) > human_curated (1.0) > hybrid_search > shared_tags (0.70)
                        best_by_target: dict[str, dict[str, Any]] = {}
                        for r in rows:
                            t_key = r["target_key"]
                            if not t_key:
                                continue
                            rel_type = str(r["relation_type"])
                            score = float(r["score"])
                            rank = (RELATION_TYPE_PRECEDENCE.get(rel_type, 0), score)

                            if t_key not in best_by_target:
                                best_by_target[t_key] = {
                                    "target_key": t_key,
                                    "score": score,
                                    "relation_type": rel_type,
                                }
                            else:
                                prev = best_by_target[t_key]
                                prev_rank = (
                                    RELATION_TYPE_PRECEDENCE.get(str(prev["relation_type"]), 0),
                                    float(prev["score"]),
                                )
                                if rank > prev_rank:
                                    best_by_target[t_key] = {
                                        "target_key": t_key,
                                        "score": score,
                                        "relation_type": rel_type,
                                    }

                        # Order by score DESC, target_key ASC with ceiling
                        sorted_targets = sorted(
                            best_by_target.values(),
                            key=lambda x: (-float(x["score"]), str(x["target_key"])),
                        )[:ceiling]

                        target_keys = [t["target_key"] for t in sorted_targets]
                        meta_by_key: dict[str, dict[str, Any]] = {}
                        if target_keys:
                            placeholders = ",".join("?" for _ in target_keys)
                            try:
                                meta_rows = conn.execute(
                                    f"SELECT item_key, title, parent_item_key FROM conversion_meta WHERE item_key IN ({placeholders})",
                                    tuple(target_keys),
                                ).fetchall()
                                for mr in meta_rows:
                                    meta_by_key[mr["item_key"]] = dict(mr)
                            except sqlite3.OperationalError:
                                pass

                        for t in sorted_targets:
                            t_key = t["target_key"]
                            mr = meta_by_key.get(t_key)
                            raw_title = mr.get("title") if mr else None
                            title = raw_title.strip() if raw_title and str(raw_title).strip() else t_key
                            raw_pk = mr.get("parent_item_key") if mr else None
                            parent_key = raw_pk.strip() if raw_pk and str(raw_pk).strip() else None

                            related_items.append(
                                {
                                    "item_key": t_key,
                                    "title": title,
                                    "relation_type": t["relation_type"],
                                    "score": round(float(t["score"]), 4),
                                    "parent_item_key": parent_key,
                                }
                            )
                except Exception as exc:
                    logger.error("Failed to query relations for %s: %s", resolved_key, exc)
                    return {"status": "error", "message": f"Database error querying relations: {exc}"}

        res: dict[str, Any] = {
            "status": "success",
            "item_key": resolved_key,
            "total_count": len(related_items),
            "related_items": related_items,
        }

        if res_notice:
            res["notice"] = res_notice

        return res
    except Exception as exc:
        logger.error("Failed in get_related_items_core for %s: %s", item_key, exc)
        return {"status": "error", "message": str(exc)}


def extract_compact_toc_outline(
    bundle_dir: Path,
    title: str = "",
    resolved_key: str = "",
    parent_key: str = "",
) -> str:
    """Extract a concise navigation outline from a document bundle.

    Strips the long abstract and retains structured sections (with level-folding for
    dense documents) and asset ranges for figures and tables.
    """
    toc_json_path = bundle_dir / "toc.json"
    if toc_json_path.exists():
        try:
            with open(toc_json_path, encoding="utf-8") as f:
                data = json.load(f)

            effective_title = (title or data.get("title") or "").strip()
            key_label = resolved_key or data.get("item_key", "")
            eff_parent_key = parent_key or data.get("parent_item_key") or ""
            if eff_parent_key and eff_parent_key != key_label:
                key_bracket = f"[Key: {key_label}, Parent Key: {eff_parent_key}]"
            else:
                key_bracket = f"[Key: {key_label}]"

            title_part = f' "{effective_title}"' if effective_title else ""
            lines = [f"### {key_bracket}{title_part}"]

            sections = data.get("sections") or []

            # Abstract line injection if available in metadata and not already an explicit section
            abstract_text = (data.get("abstract") or "").strip()
            abs_source = data.get("abstract_source") or ""
            if abstract_text and abstract_text != "No abstract or summary available." and abs_source != "none":
                has_abs_sec = any(
                    str(s.get("heading", "")).lower().strip() in ("abstract", "summary")
                    for s in sections
                )
                if not has_abs_sec:
                    lines.append(f"- [abstract] Abstract / Summary (~{len(abstract_text.split())} words)")

            if len(sections) <= 15:
                filtered_sections = sections
            else:
                filtered_sections = [s for s in sections if s.get("level", 1) <= 2]

            for sec in filtered_sections:
                s_id = sec.get("id", "")
                heading = sec.get("heading", "")
                words = sec.get("word_count", 0)
                lines.append(f"- [{s_id}] {heading} (~{words} words)")

            figures = data.get("figures") or []
            if figures:
                if len(figures) <= 8:
                    lines.append("- Figures:")
                    for f in figures:
                        caption = (f.get("caption") or "").strip()
                        lines.append(f"  - [{f.get('id')}] {caption}")
                else:
                    first_id = figures[0].get("id", "f1")
                    last_id = figures[-1].get("id", f"f{len(figures)}")
                    lines.append(
                        f"- Figures: {len(figures)} figures present ([{first_id}] to [{last_id}]). "
                        "Can be directly read via read_document(asset_id='fX') or discovered via search_corpus."
                    )

            tables = data.get("tables") or []
            if tables:
                if len(tables) <= 6:
                    lines.append("- Tables:")
                    for t in tables:
                        caption = (t.get("caption") or "").strip()
                        lines.append(f"  - [{t.get('id')}] {caption}")
                else:
                    first_id = tables[0].get("id", "t1")
                    last_id = tables[-1].get("id", f"t{len(tables)}")
                    lines.append(
                        f"- Tables: {len(tables)} tables present ([{first_id}] to [{last_id}]). "
                        "Can be directly read via read_document(asset_id='tX')."
                    )

            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to parse toc.json in %s: %s", bundle_dir, exc)

    # Fallback to toc.md if toc.json is missing or corrupted
    eff_parent_key = parent_key
    if eff_parent_key and eff_parent_key != resolved_key:
        key_bracket = f"[Key: {resolved_key}, Parent Key: {eff_parent_key}]"
    else:
        key_bracket = f"[Key: {resolved_key}]"

    toc_md_path = bundle_dir / "toc.md"
    if toc_md_path.exists():
        try:
            content = toc_md_path.read_text(encoding="utf-8")
            effective_title = title.strip()
            title_part = f' "{effective_title}"' if effective_title else ""
            lines = [f"### {key_bracket}{title_part}"]

            # Preserve content starting from ## Table of Contents
            if "## Table of Contents" in content:
                toc_body = content.split("## Table of Contents", 1)[1]
                for raw_line in toc_body.splitlines():
                    stripped = raw_line.strip()
                    if stripped.startswith("- ["):
                        lines.append(stripped)
                    elif stripped.startswith("## Figures") or stripped.startswith("## Tables"):
                        lines.append(f"- {stripped.lstrip('#').strip()}")
                    elif stripped and not stripped.startswith("#"):
                        lines.append(f"  {stripped}")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to parse toc.md fallback in %s: %s", bundle_dir, exc)

    title_part = f' "{title}"' if title else ""
    return f"### {key_bracket}{title_part}"


def _prune_cached_tocs(
    cached_tocs: dict[str, Any],
    max_entries: int = MAX_UNIQUE_CACHED_TOCS,
) -> dict[str, Any]:
    """Keep only the most recently updated unique entries, pruning by unique bundle identity."""
    unique_entries: dict[str, dict[str, Any]] = {}
    for k, v in cached_tocs.items():
        if isinstance(v, dict):
            res_key = v.get("resolved_key", k)
            if res_key not in unique_entries or v.get("updated_at", 0) > unique_entries[res_key].get("updated_at", 0):
                unique_entries[res_key] = v

    if len(unique_entries) <= max_entries:
        return cached_tocs

    sorted_unique = sorted(
        unique_entries.values(),
        key=lambda x: x.get("updated_at", 0),
        reverse=True,
    )[:max_entries]

    pruned: dict[str, Any] = {}
    for entry in sorted_unique:
        in_key = entry.get("input_key")
        res_key = entry.get("resolved_key")
        if in_key:
            pruned[in_key] = entry
        if res_key:
            pruned[res_key] = entry
    return pruned

