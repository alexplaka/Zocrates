"""
Utility script to 'un-convert' items for testing conversion status and batch conversion.

Removes the item's cached Markdown bundle (corpus/<key>/), its record
in conversion_meta.db, and its vectors/FTS entries from ChromaDB and SQLite FTS5.

Usage:
    uv run python scripts/unconvert_item.py --list
    uv run python scripts/unconvert_item.py <ITEM_KEY> [<ITEM_KEY_2> ...]
    uv run python scripts/unconvert_item.py --all

Run-by-executing mode:
    Pass the item key(s) to main() in the __main__ block (e.g. main(item_keys=["ABC12345"])),
    then run `uv run python scripts/unconvert_item.py` with no arguments.
"""

import sys
from pathlib import Path

from zocrates.config import (
    get_chroma_dir,
    get_corpus_dir,
    get_fts_db_path,
)
from zocrates.rag.document_processor import DocumentProcessor
from zocrates.rag.embeddings import get_embedder
from zocrates.rag.vector_store import VectorStore
from zocrates.rag.zotero_db import ZoteroDB

# ─── Run-by-executing mode ─────────────────────────────────────────────────────
# Specify the item key(s) in the main() call inside the __main__ block at the
# bottom of this file, then run the script with no arguments:
#     uv run python scripts/unconvert_item.py
# CLI arguments still take precedence when provided.


def find_zotero_dir() -> Path:
    """Locate the active Zotero profile directory containing zocrates-data."""
    zotero_dirs = list(Path.home().glob("Zotero*/zocrates-data")) + list(
        Path.home().glob("Zotero*/*/zocrates-data")
    )
    if not zotero_dirs:
        print("Error: Could not locate zocrates-data directory in ~/Zotero*")
        sys.exit(1)
    return zotero_dirs[0].parent


def list_converted_items(processor: DocumentProcessor, db: ZoteroDB) -> None:
    """List all currently converted and cached items."""
    cached_keys = processor.list_cached_keys()
    if not cached_keys:
        print("No converted items found in cache.")
        return

    print(f"\n📦 Found {len(cached_keys)} converted item(s) in corpus:\n")
    print(f"{'Item Key':<12} {'Engine':<16} {'Quality':<10} {'Title / Filename'}")
    print("-" * 75)

    for key in sorted(cached_keys):
        meta = processor.get_metadata(key) or {}
        engine = meta.get("converter_id", "unknown")
        score = meta.get("parse_quality_score")
        score_str = f"{score:.2f}/5" if score is not None else "-"
        
        # Try to resolve title from DB
        title = meta.get("title") or ""
        if not title:
            item = db.get_item_by_key(key)
            if item:
                title = item.title
            else:
                att = db.get_attachment_by_key(key)
                if att and att.path:
                    title = att.path.name
                elif att and att.parent_item_key:
                    p_item = db.get_item_by_key(att.parent_item_key)
                    title = p_item.title if p_item else key
                else:
                    title = key

        title_trunc = title[:35] + "..." if len(title) > 35 else title
        print(f"{key:<12} {engine:<16} {score_str:<10} {title_trunc}")
    print()


def unconvert_item(
    key: str,
    processor: DocumentProcessor,
    store: VectorStore,
    db: ZoteroDB,
) -> bool:
    """Un-convert a single item by key (supports parent item keys or attachment keys)."""
    # 1. Resolve target keys (could be a parent item with attachment, or direct attachment)
    target_keys = []
    if processor.is_cached(key):
        target_keys.append(key)
    
    # Check if key is a parent item with child attachments in cache
    item = db.get_item_by_key(key)
    if item:
        atts = db.get_attachments(item.item_id)
        for a in atts:
            if processor.is_cached(a.item_key) and a.item_key not in target_keys:
                target_keys.append(a.item_key)

    if not target_keys:
        if processor.is_cached(key):
            target_keys.append(key)
        else:
            print(f"⚠️  Item '{key}' is not currently cached/converted.")
            return False

    for target_key in target_keys:
        print(f"🗑️  Un-converting [{target_key}]...")
        # Remove from vector store (Chroma + SQLite FTS5)
        store.delete_document(target_key)
        # Remove cached bundle and metadata record
        processor.delete_cached(target_key)
        print(f"   ✅ Removed cache, metadata, and vectors for [{target_key}]")

    return True


def main(item_keys: str | list[str] | None = None) -> None:
    """Un-convert items.

    item_keys: Item key or list of item keys to un-convert in run-by-executing
        mode, used only when no CLI arguments are given. Accepts a single key
        string or a list of key strings for parity with the CLI arguments.
    """
    args = sys.argv[1:]
    # Run-by-executing mode: fall back to the item_keys parameter when no CLI args are given.
    if not args and item_keys:
        if isinstance(item_keys, str):
            item_keys = [item_keys]
        args = list(item_keys)
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)

    zotero_dir = find_zotero_dir()
    cache_dir = get_corpus_dir(zotero_dir)
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"

    processor = DocumentProcessor(cache_dir)
    embedder = get_embedder()
    store = VectorStore(
        chroma_dir=get_chroma_dir(zotero_dir),
        fts_db_path=get_fts_db_path(zotero_dir),
        embedder=embedder,
    )
    db = ZoteroDB(sqlite_path, storage_dir)

    if "--list" in args:
        list_converted_items(processor, db)
        return

    if "--all" in args:
        cached_keys = processor.list_cached_keys()
        if not cached_keys:
            print("No converted items to un-convert.")
            return
        confirm = input(f"Are you sure you want to un-convert ALL {len(cached_keys)} items? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return
        for key in cached_keys:
            unconvert_item(key, processor, store, db)
        print(f"\n✨ Successfully un-converted all {len(cached_keys)} items.")
        return

    # Un-convert specific keys
    for key in args:
        clean_key = key.strip().upper()
        if clean_key:
            unconvert_item(clean_key, processor, store, db)


if __name__ == "__main__":
    # ─── Run-by-executing mode ───
    # Replace the empty list below with the item key(s), e.g.
    #     main(item_keys=["ABC12345"])
    #     main(item_keys=["ABC12345", "XYZ67890"])
    main(item_keys=[])
