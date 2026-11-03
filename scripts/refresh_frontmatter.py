"""
CLI wrapper to refresh/backfill YAML frontmatter in cached Markdown bundles.

Delegates core execution to ``app.rag.frontmatter_refresher`` to synchronize
OKF v0.2 frontmatter with the latest bibliographic metadata (authors, year,
publication, DOI, URL) from Zotero without re-running document conversion or OCR.

Usage:
    uv run python scripts/refresh_frontmatter.py
    uv run python scripts/refresh_frontmatter.py <ITEM_KEY> [<ITEM_KEY_2> ...]
    uv run python scripts/refresh_frontmatter.py --dry-run
    uv run python scripts/refresh_frontmatter.py --stale-only
    uv run python scripts/refresh_frontmatter.py --sanitize-images
    uv run python scripts/refresh_frontmatter.py --data-dir "/path/to/Zotero"
"""

import argparse
import sys
from pathlib import Path

from zocrates.config import get_corpus_dir
from zocrates.rag.frontmatter_refresher import (
    refresh_bundle_frontmatter,
    sync_stale_metadata,
)


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


def refresh_relations(
    zotero_dir: Path,
    target_keys: list[str] | None = None,
    dry_run: bool = False,
    syncer: object | None = None,
) -> dict[str, int]:
    """Sync explicit manual/tag relations and update item_relations.md for cached bundles.

    Args:
        zotero_dir: Root Zotero data directory.
        target_keys: Optional list of item keys to process (default: all in conversion_meta.db).
        dry_run: If True, preview actions without modifying files or DB.
        syncer: Optional active LibrarySyncer or mock syncer instance.

    Returns:
        Dict with 'processed' and 'regenerated' counts.
    """
    import json
    from zocrates.rag.bundle_catalog import regenerate_item_relations_md
    from zocrates.rag.document_processor import _open_meta_db
    from zocrates.rag.graph_engine import (
        evaluate_and_persist_edges,
        sync_manual_relations,
        sync_shared_tags,
    )
    from zocrates.rag.keyword_extractor import (
        extract_statistical_keywords,
        extract_structural_digest,
    )

    cache_dir = get_corpus_dir(zotero_dir)
    meta_db_path = cache_dir / "conversion_meta.db"
    if not meta_db_path.exists():
        return {"processed": 0, "regenerated": 0}

    sqlite_path = zotero_dir / "zotero.sqlite"

    with _open_meta_db(cache_dir) as meta_conn:
        if not dry_run:
            sync_manual_relations(meta_conn, zotero_dir)
            sync_shared_tags(meta_conn, zotero_dir)

        if target_keys:
            placeholders = ",".join("?" for _ in target_keys)
            rows = meta_conn.execute(
                f"SELECT item_key, title, abstract, keywords FROM conversion_meta WHERE item_key IN ({placeholders})",
                tuple(target_keys),
            ).fetchall()
        else:
            rows = meta_conn.execute(
                "SELECT item_key, title, abstract, keywords FROM conversion_meta"
            ).fetchall()

        active_syncer = syncer
        own_syncer = False
        if active_syncer is None and not dry_run:
            try:
                from zocrates.rag.library_syncer import LibrarySyncer

                active_syncer = LibrarySyncer(
                    zotero_data_dir=zotero_dir, sqlite_path=sqlite_path
                )
                own_syncer = True
            except Exception:
                active_syncer = None

        processed_count = 0
        regenerated_count = 0

        try:
            for r in rows:
                item_key = r["item_key"]
                title = str(r["title"] or "")
                abstract = str(r["abstract"] or "")
                kw_raw = r["keywords"]

                keywords: list[str] = []
                if kw_raw:
                    try:
                        parsed = json.loads(kw_raw)
                        if isinstance(parsed, list):
                            keywords = [str(k) for k in parsed]
                    except Exception:
                        keywords = []

                if not keywords:
                    bundle_dir = cache_dir / item_key
                    doc_path = bundle_dir / "document.md"
                    content = ""
                    if doc_path.exists():
                        content = doc_path.read_text(encoding="utf-8")
                    elif abstract:
                        content = abstract

                    if content:
                        digest = extract_structural_digest(content)
                        keywords = extract_statistical_keywords(
                            title, digest, max_keywords=6
                        )
                        if keywords and not dry_run:
                            meta_conn.execute(
                                "UPDATE conversion_meta SET keywords = ? WHERE item_key = ?",
                                (json.dumps(keywords), item_key),
                            )
                            meta_conn.commit()

                if not dry_run:
                    if active_syncer is not None:
                        try:
                            evaluate_and_persist_edges(
                                item_key, syncer=active_syncer, sync_bundle_files=True
                            )
                        except Exception:
                            pass

                    reg_path = regenerate_item_relations_md(
                        item_key=item_key, cache_dir=cache_dir
                    )
                    if reg_path is not None:
                        regenerated_count += 1
                else:
                    regenerated_count += 1

                processed_count += 1
        finally:
            if own_syncer and active_syncer is not None and hasattr(active_syncer, "close"):
                active_syncer.close()

        return {"processed": processed_count, "regenerated": regenerated_count}


def refresh_frontmatters(
    zotero_dir: Path,
    target_keys: list[str] | None = None,
    dry_run: bool = False,
    sanitize_images: bool = False,
) -> dict[str, int]:
    """Refresh YAML frontmatters for target items or all cached items.

    Backward-compatible bridge function delegating to ``refresh_bundle_frontmatter``.
    """
    return refresh_bundle_frontmatter(
        zotero_dir=zotero_dir,
        target_keys=target_keys,
        dry_run=dry_run,
        sanitize_images=sanitize_images,
    )


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for frontmatter refresh."""
    parser = argparse.ArgumentParser(
        description="Refresh YAML frontmatters in cached Markdown bundles without reconversion."
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
        "--sanitize-images",
        action="store_true",
        help="Sanitize legacy absolute file:///.../images/ URIs in cached document.md files to relative images/.",
    )
    parser.add_argument(
        "--stale-only",
        "-s",
        action="store_true",
        help="Only refresh cached bundles whose Zotero metadata has been modified since conversion.",
    )
    parser.add_argument(
        "--refresh-relations",
        action="store_true",
        help="Sync explicit Zotero manual/tag relations and regenerate item_relations.md for cached bundles.",
    )

    args = parser.parse_args(argv)
    zotero_dir = find_zotero_dir(args.data_dir)
    print(f"Target Zotero Data Directory: {zotero_dir}")

    sqlite_path = zotero_dir / "zotero.sqlite"
    if not sqlite_path.exists():
        print(f"❌ Error: Zotero database not found at {sqlite_path}")
        sys.exit(1)

    cache_dir = get_corpus_dir(zotero_dir)
    meta_db = cache_dir / "conversion_meta.db"
    if not meta_db.exists():
        print(f"❌ Error: No conversion_meta.db found at {meta_db}")
        sys.exit(1)

    if args.refresh_relations:
        target_keys = args.item_keys if args.item_keys else None
        if target_keys:
            print(f"Targeting relations for {len(target_keys)} item key(s): {', '.join(target_keys)}")
        else:
            print("Refreshing relations across all cached bundles...")

        res = refresh_relations(
            zotero_dir=zotero_dir,
            target_keys=target_keys,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(
                f"\nDry-run complete: {res['processed']} item(s) would be processed, "
                f"{res['regenerated']} file(s) would be regenerated."
            )
        else:
            print(
                f"\nRelations refresh complete: {res['processed']} items processed, "
                f"{res['regenerated']} files regenerated."
            )
        return

    if args.stale_only:
        print(f"Checking for stale metadata across cached bundles (dry_run={args.dry_run})...")
        res = sync_stale_metadata(
            zotero_dir=zotero_dir,
            dry_run=args.dry_run,
            sanitize_images=args.sanitize_images,
        )
        if args.dry_run:
            print(
                f"\nDry-run complete: {res['updated']} item(s) would be updated, "
                f"{res['skipped']} skipped, {res['errored']} errored (total scanned: {res['total']})."
            )
        else:
            print(
                f"Staleness sync complete: {res['updated']} updated, {res['skipped']} skipped, "
                f"{res['errored']} errored (total scanned: {res['total']})."
            )
    else:
        target_keys = args.item_keys if args.item_keys else None
        if target_keys:
            print(f"Targeting {len(target_keys)} item key(s): {', '.join(target_keys)}")
        else:
            print("Scanning all cached bundles...")

        res = refresh_bundle_frontmatter(
            zotero_dir=zotero_dir,
            target_keys=target_keys,
            dry_run=args.dry_run,
            sanitize_images=args.sanitize_images,
        )
        if args.dry_run:
            print(
                f"\nDry-run complete: {res['updated']} item(s) would be updated, "
                f"{res['skipped']} skipped, {res['errored']} errored (total: {res['total']})."
            )
        else:
            print(
                f"\nFrontmatter refresh complete: {res['updated']} updated, "
                f"{res['skipped']} skipped, {res['errored']} errored (total: {res['total']})."
            )


if __name__ == "__main__":
    main()
