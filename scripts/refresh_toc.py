"""
Utility script to refresh, backfill, or regenerate Table of Contents (ToC)
and abstract/summary metadata in cached Markdown bundles.

Generates dual-format `toc.json` and `toc.md` files containing sections,
figures, tables, and abstract/summary data for existing converted bundles
without re-running converter engines.

Usage:
    uv run python scripts/refresh_toc.py
    uv run python scripts/refresh_toc.py <ITEM_KEY> [<ITEM_KEY_2> ...]
    uv run python scripts/refresh_toc.py --dry-run
    uv run python scripts/refresh_toc.py --data-dir "/path/to/Zotero"
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from zocrates.rag.bundle_catalog import generate_item_index_md
from zocrates.config import get_corpus_dir
from zocrates.rag.summary_generator import generate_summary
from zocrates.rag.toc_generator import generate_toc, save_toc_files
from zocrates.rag.zotero_db import ZoteroDB


def find_zotero_dir(explicit_dir: str | None = None) -> Path:
    """Locate the active Zotero profile directory containing zocrates-data."""
    if explicit_dir:
        p = Path(explicit_dir)
        if p.exists():
            return p
        print(f"Error: Specified data directory does not exist: {explicit_dir}")
        sys.exit(1)

    zotero_dirs = list(Path.home().glob("Zotero*/zocrates-data")) + list(
        Path.home().glob("Zotero*/*/zocrates-data")
    )
    if not zotero_dirs:
        print("Error: Could not locate zocrates-data directory in ~/Zotero*")
        sys.exit(1)
    return zotero_dirs[0].parent


def refresh_tocs(
    zotero_dir: Path,
    target_keys: list[str] | None = None,
    dry_run: bool = False,
    force_summary: bool = False,
) -> None:
    """Refresh Table of Contents and abstract metadata for target items or all cached items."""
    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    cache_dir = get_corpus_dir(zotero_dir)
    meta_db_path = cache_dir / "conversion_meta.db"

    if not sqlite_path.exists():
        print(f"❌ Error: Zotero database not found at {sqlite_path}")
        sys.exit(1)

    if not meta_db_path.exists():
        print(f"❌ Error: No conversion_meta.db found at {meta_db_path}")
        sys.exit(1)

    db = ZoteroDB(sqlite_path, storage_dir)
    meta_conn = sqlite3.connect(
        f"file:{meta_db_path}?mode=ro" if dry_run else str(meta_db_path),
        uri=dry_run,
    )
    meta_conn.row_factory = sqlite3.Row

    # Ensure columns exist in DB if not dry run
    if not dry_run:
        existing_cols = {
            row["name"]
            for row in meta_conn.execute("PRAGMA table_info(conversion_meta)").fetchall()
        }
        if "abstract" not in existing_cols:
            meta_conn.execute("ALTER TABLE conversion_meta ADD COLUMN abstract TEXT;")
        if "abstract_source" not in existing_cols:
            meta_conn.execute("ALTER TABLE conversion_meta ADD COLUMN abstract_source TEXT;")
        meta_conn.commit()

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
        print("No matching items found in conversion_meta.db.")
        return

    print(f"Found {len(rows)} cached item(s) to process (dry_run={dry_run})...\n")

    updated = 0
    skipped = 0

    for r in rows:
        item_key = r["item_key"]
        parent_key = r["parent_item_key"]
        bundle_dir = cache_dir / item_key
        doc_path = bundle_dir / "document.md"

        if not doc_path.exists():
            print(f"⚠️  [{item_key}] document.md not found on disk; skipping.")
            skipped += 1
            continue

        # Look up parent item in Zotero DB (handling attachments correctly)
        parent_item = None
        if parent_key:
            parent_item = db.get_item_by_key(parent_key)

        if not parent_item:
            att = db.get_attachment_by_key(item_key)
            if att:
                if att.parent_item_id:
                    parent_item = db.get_item(att.parent_item_id)
                elif att.parent_item_key:
                    parent_item = db.get_item_by_key(att.parent_item_key)
                if not parent_key and att.parent_item_key:
                    parent_key = att.parent_item_key

        if not parent_item:
            parent_item = db.get_item_by_key(item_key)

        title = (
            parent_item.title
            if parent_item and parent_item.title
            else (r["title"] or "Untitled")
        )

        # Abstract resolution hierarchy: Zotero DB -> existing in conversion_meta -> LLM/extractive summary
        abstract = None
        abstract_source = "zotero"

        if not force_summary and parent_item and parent_item.abstract and parent_item.abstract.strip():
            abstract = parent_item.abstract.strip()
            abstract_source = "zotero"
        elif not force_summary and "abstract" in r.keys() and r["abstract"] and str(r["abstract"]).strip():
            abstract = str(r["abstract"]).strip()
            abstract_source = r["abstract_source"] if "abstract_source" in r.keys() and r["abstract_source"] else "generated"

        raw_md = doc_path.read_text(encoding="utf-8")

        if not abstract:
            print(f"🔄 [{item_key}] Generating summary for '{title[:35]}...'")
            abstract, abstract_source = generate_summary(
                raw_md, title=title, zotero_data_dir=zotero_dir
            )

        toc_json, toc_md = generate_toc(
            document_md=raw_md,
            title=title,
            abstract=abstract,
            abstract_source=abstract_source,
            item_key=item_key,
            parent_item_key=parent_key,
        )

        sec_count = len(toc_json.get("sections", []))
        fig_count = len(toc_json.get("figures", []))
        tbl_count = len(toc_json.get("tables", []))

        print(
            f"✓ [{item_key}] {title[:40]:<40} (sections={sec_count}, figures={fig_count}, tables={tbl_count}, abs_src={abstract_source})"
        )

        if not dry_run:
            save_toc_files(bundle_dir, toc_json, toc_md)
            generate_item_index_md(
                bundle_dir,
                item_key,
                title,
                r["converter_id"],
                r["converter_version"],
            )
            meta_conn.execute(
                """
                UPDATE conversion_meta
                SET abstract = ?, abstract_source = ?
                WHERE item_key = ?
                """,
                (abstract, abstract_source, item_key),
            )
            updated += 1
        else:
            updated += 1

    if not dry_run:
        meta_conn.commit()
        print(f"\n🎉 Successfully generated/refreshed ToC for {updated}/{len(rows)} item(s).")
    else:
        print(f"\n🎉 Dry-run complete. {updated} item(s) would be updated.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh or backfill ToC files and summaries in cached Markdown bundles."
    )
    parser.add_argument(
        "item_keys",
        nargs="*",
        help="Optional specific Zotero item key(s) to refresh (default: all cached items).",
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=str,
        default=None,
        help="Path to Zotero data directory (default: auto-detected).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without writing to disk or DB.",
    )
    parser.add_argument(
        "--force-summary",
        "-f",
        action="store_true",
        help="Force regeneration of summary even if an abstract already exists.",
    )

    args = parser.parse_args()
    zotero_dir = find_zotero_dir(args.data_dir)
    print(f"Target Zotero Data Directory: {zotero_dir}")

    target_keys = args.item_keys if args.item_keys else None
    refresh_tocs(
        zotero_dir,
        target_keys=target_keys,
        dry_run=args.dry_run,
        force_summary=args.force_summary,
    )


if __name__ == "__main__":
    main()
