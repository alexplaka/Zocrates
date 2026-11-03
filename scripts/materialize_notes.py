"""Batch-materialize OKF note bundles for all notes in the Zotero library.

Creates ``document.md``, ``toc.json``, and ``toc.md`` under
``<zocrates-data>/corpus/<note_key>/`` for every note (child and
standalone) so they are accessible via ``get_document_toc`` / ``read_document``.

Usage:
    uv run python scripts/materialize_notes.py
    uv run python scripts/materialize_notes.py --data-dir "/path/to/Zotero"
    uv run python scripts/materialize_notes.py --force
"""

import argparse
import shutil
from pathlib import Path

from zocrates.config import resolve_zotero_data_dir
from zocrates.rag.document_processor import materialize_note_bundle
from zocrates.rag.service_manager import get_syncer, initialize_services


def find_zotero_dir(explicit_dir: str | None = None) -> Path:
    """Locate the active Zotero data directory containing zotero.sqlite."""
    if explicit_dir:
        p = Path(explicit_dir)
        if not p.exists():
            print(f"Error: Specified data directory does not exist: {explicit_dir}")
            raise SystemExit(1)
        if not (p / "zotero.sqlite").exists():
            print(f"Error: Specified data directory does not contain zotero.sqlite: {explicit_dir}")
            raise SystemExit(1)
        return p

    resolved = resolve_zotero_data_dir()
    if resolved is not None and resolved.exists():
        if (resolved / "zotero.sqlite").exists():
            return resolved
        print(f"Error: Zotero directory exists but contains no zotero.sqlite: {resolved}")
        raise SystemExit(1)

    print("Error: Could not locate Zotero data directory.")
    print("Start the Zotero plugin (or set ZOTERO_DATA_DIR) and retry, or pass --data-dir.")
    raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Materialize OKF note bundles for all notes in the Zotero library.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-d", "--data-dir",
        default=None,
        help="Path to Zotero data directory (default: auto-detected).",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Re-materialize all notes even if a bundle already exists.",
    )
    return parser


def materialize_all_notes(zotero_dir: Path, force: bool = False) -> int:
    """Materialize OKF bundles for every note in the library.

    Args:
        zotero_dir: Zotero data directory containing ``zotero.sqlite``.
        force: When True, delete existing note bundles first so they are
            rebuilt from the current note HTML.

    Returns:
        Number of notes successfully materialized.
    """
    initialize_services(zotero_dir)
    syncer = get_syncer()
    notes = syncer.db.get_all_notes()

    print(f"Found {len(notes)} note(s) in the Zotero library.")
    if not notes:
        print("No notes to materialize.")
        return 0

    success = 0
    for idx, note in enumerate(notes, 1):
        try:
            if force:
                bundle_dir = syncer.processor._cache_dir / note.note_key
                if bundle_dir.exists():
                    shutil.rmtree(bundle_dir, ignore_errors=True)
            materialize_note_bundle(note, syncer)
            success += 1
        except Exception as exc:
            print(f"  ❌ [{note.note_key}] Materialization failed: {exc}")

    print(f"\nSuccessfully materialized {success}/{len(notes)} note bundle(s).")
    return success


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    zotero_dir = find_zotero_dir(args.data_dir)
    print(f"Using Zotero data directory: {zotero_dir}")

    success = materialize_all_notes(zotero_dir, force=args.force)
    syncer = get_syncer()
    total = len(syncer.db.get_all_notes()) if (syncer and syncer.db) else 0
    if total > 0 and success < total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
