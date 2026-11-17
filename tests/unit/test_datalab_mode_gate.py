"""Unit tests for the mode-aware conversion identity, metadata and migration.

Covers the plan §3.4 metadata/gate work:
  - ``matches_conversion_identity`` mode-aware gate truth table.
  - ``conversion_meta.converter_mode`` schema, ALTER migration, the once-only
    ``user_version = 2`` backfill, and the ``get_metadata`` /
    ``get_all_metadata`` projections.
  - ``_save_to_cache`` recording the surviving Datalab mode independently of the
    quality/checkpoint conditional, with ``datalab_document.json`` as the
    post-restart recovery source.
  - ``convert_with(skip_converted=True)`` suppressing duplicate paid work under
    the per-item lock.
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from zocrates.rag.document_converters import BaseDocumentConverter, DatalabConverter
from zocrates.rag.document_processor import (
    _INITIALIZED_META_DBS,
    _file_sha256,
    _open_meta_db,
    DocumentProcessor,
    matches_conversion_identity,
)

LEGACY_META_SCHEMA = """
CREATE TABLE conversion_meta (
    item_key            TEXT PRIMARY KEY,
    converter_id        TEXT NOT NULL,
    converter_version   TEXT NOT NULL,
    source_path         TEXT NOT NULL,
    content_type        TEXT NOT NULL,
    parent_item_type    TEXT,
    converted_at        TEXT NOT NULL,
    indexed_at          TEXT,
    source_hash         TEXT NOT NULL,
    char_count          INTEGER NOT NULL,
    parent_item_key     TEXT
);
"""


# ---------------------------------------------------------------------------
# Gate predicate truth table
# ---------------------------------------------------------------------------
class TestMatchesConversionIdentity:
    """Mode-aware already-converted predicate (plan §3.4)."""

    def _meta(self, mode: str | None, converter_id: str = "datalab", source_hash: str = "hash1") -> dict:
        """Build a metadata dict shaped like ``get_metadata`` output."""
        return {
            "converter_id": converter_id,
            "converter_mode": mode,
            "source_hash": source_hash,
        }

    def test_matching_mode_and_hash_is_already_converted(self):
        assert matches_conversion_identity(self._meta("balanced"), "datalab", "balanced", "hash1")

    def test_different_stored_mode_is_eligible(self):
        """A 'fast' row no longer blocks a 'balanced' request."""
        assert not matches_conversion_identity(self._meta("fast"), "datalab", "balanced", "hash1")
        assert not matches_conversion_identity(self._meta("accurate"), "datalab", "fast", "hash1")

    @pytest.mark.parametrize("requested", ["fast", "balanced", "accurate"])
    def test_stored_null_mode_matches_any_mode(self, requested):
        """Legacy rows and non-Datalab converters store NULL, which matches any mode."""
        assert matches_conversion_identity(self._meta(None), "datalab", requested, "hash1")

    def test_different_converter_is_eligible(self):
        assert not matches_conversion_identity(self._meta(None, "markitdown"), "datalab", "balanced", "hash1")

    def test_hash_mismatch_is_eligible(self):
        assert not matches_conversion_identity(self._meta("balanced"), "datalab", "balanced", "other-hash")

    def test_uncomputed_hash_is_eligible(self):
        assert not matches_conversion_identity(self._meta("balanced"), "datalab", "balanced", None)

    def test_missing_metadata_is_eligible(self):
        assert not matches_conversion_identity(None, "datalab", "balanced", "hash1")


# ---------------------------------------------------------------------------
# Schema, migration and projections
# ---------------------------------------------------------------------------
class TestConverterModeMigration:
    """``converter_mode`` column addition, once-only backfill and projections."""

    def _columns(self, cache_dir: Path) -> set[str]:
        """Return the conversion_meta column names for ``cache_dir``."""
        conn = sqlite3.connect(str(cache_dir / "conversion_meta.db"))
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(conversion_meta)")}
        finally:
            conn.close()

    def test_fresh_database_has_converter_mode_column(self, tmp_path):
        cache_dir = tmp_path / "cache"
        processor = DocumentProcessor(cache_dir)
        assert processor.get_metadata("MISSING") is None

        assert "converter_mode" in self._columns(cache_dir)
        conn = sqlite3.connect(str(cache_dir / "conversion_meta.db"))
        try:
            assert conn.execute("PRAGMA user_version;").fetchone()[0] == 2
        finally:
            conn.close()

    def test_legacy_database_gains_column_and_balanced_backfill(self, tmp_path):
        cache_dir = tmp_path / "legacy"
        cache_dir.mkdir()
        db_path = cache_dir / "conversion_meta.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(LEGACY_META_SCHEMA)
            conn.execute(
                "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,"
                " content_type, converted_at, source_hash, char_count) "
                "VALUES ('LEGACY_DL', 'datalab', '0.7.1', 'storage/LEGACY_DL/paper.pdf',"
                " 'application/pdf', '2025-01-01T00:00:00+00:00', 'hash-dl', 100)"
            )
            conn.execute(
                "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,"
                " content_type, converted_at, source_hash, char_count) "
                "VALUES ('LEGACY_MD', 'markitdown', '0.1.0', 'storage/LEGACY_MD/page.html',"
                " 'text/html', '2025-01-01T00:00:00+00:00', 'hash-md', 50)"
            )
            conn.commit()
        finally:
            conn.close()

        _INITIALIZED_META_DBS.discard(cache_dir)
        processor = DocumentProcessor(cache_dir)

        # Legacy Datalab rows were produced by code whose intended default was balanced.
        assert processor.get_metadata("LEGACY_DL")["converter_mode"] == "balanced"
        assert "converter_mode" in self._columns(cache_dir)
        # Non-Datalab rows stay NULL.
        assert processor.get_metadata("LEGACY_MD")["converter_mode"] is None

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version;").fetchone()[0] == 2
        finally:
            conn.close()

    def test_version_1_database_is_not_re_relativized(self, tmp_path):
        """A DB already at user_version 1 only gets the new column and backfill."""
        cache_dir = tmp_path / "v1"
        cache_dir.mkdir()
        db_path = cache_dir / "conversion_meta.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(LEGACY_META_SCHEMA)
            conn.execute(
                "INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,"
                " content_type, converted_at, source_hash, char_count) "
                "VALUES ('V1_DL', 'datalab', '0.10.0', 'storage/V1_DL/paper.pdf',"
                " 'application/pdf', '2025-01-01T00:00:00+00:00', 'hash-v1', 10)"
            )
            conn.execute("PRAGMA user_version = 1;")
            conn.commit()
        finally:
            conn.close()

        _INITIALIZED_META_DBS.discard(cache_dir)
        processor = DocumentProcessor(cache_dir)

        assert processor.get_metadata("V1_DL")["converter_mode"] == "balanced"
        assert not (cache_dir / "conversion_meta.db.bak-pre-relativization").exists()
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version;").fetchone()[0] == 2
        finally:
            conn.close()

    def test_backfill_runs_exactly_once(self, tmp_path):
        """After the version stamp, an intentional NULL datalab mode survives reopening."""
        cache_dir = tmp_path / "cache"
        processor = DocumentProcessor(cache_dir)
        source = tmp_path / "paper.pdf"
        source.write_text("pdf bytes", encoding="utf-8")
        processor.get_or_convert("ONCE1", source, "application/pdf")

        conn = sqlite3.connect(str(cache_dir / "conversion_meta.db"))
        try:
            conn.execute("UPDATE conversion_meta SET converter_mode = NULL WHERE item_key = 'ONCE1'")
            conn.commit()
            assert conn.execute("PRAGMA user_version;").fetchone()[0] == 2
        finally:
            conn.close()

        _INITIALIZED_META_DBS.discard(cache_dir)
        reopened = DocumentProcessor(cache_dir)
        assert reopened.get_metadata("ONCE1")["converter_mode"] is None

    def test_projections_surface_converter_mode(self, tmp_path):
        cache_dir = tmp_path / "cache"
        processor = DocumentProcessor(cache_dir, converters=[HookModeConverter("fast")])
        source = tmp_path / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 bytes")

        processor.convert_with(
            "PROJ1", "datalab", source, "application/pdf", options={"mode": "fast"}
        )

        assert processor.get_metadata("PROJ1")["converter_mode"] == "fast"
        assert processor.get_all_metadata()["PROJ1"]["converter_mode"] == "fast"

    def test_non_datalab_conversion_records_null_mode(self, tmp_path):
        cache_dir = tmp_path / "cache"
        processor = DocumentProcessor(cache_dir)
        source = tmp_path / "notes.md"
        source.write_text("# Notes", encoding="utf-8")

        processor.get_or_convert("NONDL1", source, "text/markdown")

        assert processor.get_metadata("NONDL1")["converter_mode"] is None


# ---------------------------------------------------------------------------
# Write path: _save_to_cache mode resolution
# ---------------------------------------------------------------------------
class HookModeConverter(DatalabConverter):
    """DatalabConverter that records an in-memory hook instead of calling the API."""

    def __init__(self, mode: str, score: float | None = None, checkpoint: str | None = None) -> None:
        super().__init__()
        self.mode = mode
        self.score = score
        self.checkpoint = checkpoint
        self.calls = 0

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> str | None:
        self.calls += 1
        resolved = self.mode
        if isinstance(options, dict) and options.get("mode"):
            resolved = options["mode"]
        self._metadata[item_key] = {
            "parse_quality_score": self.score,
            "checkpoint_id": self.checkpoint,
            "mode": resolved,
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "datalab_document.json").write_text(
            json.dumps(
                {
                    "parse_quality_score": self.score,
                    "checkpoint_id": self.checkpoint,
                    "mode": resolved,
                }
            ),
            encoding="utf-8",
        )
        return f"# Converted {source_path.name} in {resolved} mode"


class TestSaveToCacheConverterMode:
    """Mode recording must not inherit the quality/checkpoint conditional."""

    def test_mode_persists_when_score_and_checkpoint_are_missing(self, tmp_path):
        cache_dir = tmp_path / "cache"
        processor = DocumentProcessor(cache_dir, converters=[HookModeConverter("balanced")])
        source = tmp_path / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 bytes")

        processor.convert_with(
            "NOMODE1", "datalab", source, "application/pdf", options={"mode": "accurate"}
        )

        meta = processor.get_metadata("NOMODE1")
        assert meta["parse_quality_score"] is None
        assert meta["checkpoint_id"] is None
        assert meta["converter_mode"] == "accurate"

    def test_datalab_document_json_recovers_mode_after_restart(self, tmp_path):
        """With an empty in-memory hook, the mode is recovered from datalab_document.json."""
        cache_dir = tmp_path / "cache"
        converter = HookModeConverter("balanced")
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 bytes")

        processor.convert_with(
            "RESTART1", "datalab", source, "application/pdf", options={"mode": "accurate"}
        )
        assert (cache_dir / "RESTART1" / "datalab_document.json").exists()

        # Simulate a backend restart: fresh processor, cold in-memory hook.
        restarted = DocumentProcessor(cache_dir, converters=[HookModeConverter("balanced")])
        assert restarted._get_converter_by_id("datalab").get_metadata("RESTART1") == {}

        restarted._save_to_cache(
            "RESTART1",
            source,
            "application/pdf",
            _file_sha256(source),
            "# Recovered mode",
            "datalab",
            "0.10.0",
        )

        assert restarted.get_metadata("RESTART1")["converter_mode"] == "accurate"

    def test_hook_mode_takes_precedence_over_json(self, tmp_path):
        cache_dir = tmp_path / "cache"
        converter = HookModeConverter("balanced")
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 bytes")

        processor.convert_with(
            "PRECEDE1", "datalab", source, "application/pdf", options={"mode": "fast"}
        )
        # The hook says fast while the bundle JSON is stale at balanced.
        (cache_dir / "PRECEDE1" / "datalab_document.json").write_text(
            '{"mode": "balanced"}', encoding="utf-8"
        )

        processor._save_to_cache(
            "PRECEDE1", source, "application/pdf", _file_sha256(source), "# Doc", "datalab", "0.10.0"
        )

        assert processor.get_metadata("PRECEDE1")["converter_mode"] == "fast"


# ---------------------------------------------------------------------------
# In-lock gate recheck
# ---------------------------------------------------------------------------
class CountingConverter(BaseDocumentConverter):
    """Converter that counts invocations and sleeps long enough to overlap."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self.calls = 0
        self._guard = threading.Lock()

    @property
    def converter_id(self) -> str:
        return "counting"

    def supports(self, content_type: str, source_path: Path) -> bool:
        return True

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> str | None:
        import time

        with self._guard:
            self.calls += 1
        time.sleep(self.hold_seconds)
        return "# Counting conversion"


class TestSkipConvertedRecheck:
    """``convert_with(skip_converted=True)`` suppresses duplicate same-mode work."""

    def _run_concurrently(self, processor: DocumentProcessor, source: Path, **kwargs) -> list[str]:
        """Run two ``convert_with`` calls for one item behind a barrier."""
        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(
                    processor.convert_with(
                        "DUP1", "counting", source, "application/pdf",
                        skip_converted=True, **kwargs,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        return results

    def test_concurrent_non_forced_requests_convert_once(self, tmp_path):
        cache_dir = tmp_path / "cache"
        converter = CountingConverter()
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        results = self._run_concurrently(processor, source)

        assert converter.calls == 1, "the in-lock recheck must suppress duplicate paid work"
        assert len(results) == 2
        assert all("# Counting conversion" in result for result in results)

    def test_skip_path_returns_cached_markdown(self, tmp_path):
        cache_dir = tmp_path / "cache"
        converter = CountingConverter(hold_seconds=0.0)
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        first = processor.convert_with("CACHED1", "counting", source, "application/pdf")
        second = processor.convert_with(
            "CACHED1", "counting", source, "application/pdf", skip_converted=True
        )

        assert converter.calls == 1
        assert second == first
        assert "# Counting conversion" in second

    def test_skip_converted_false_still_converts(self, tmp_path):
        cache_dir = tmp_path / "cache"
        converter = CountingConverter(hold_seconds=0.0)
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        processor.convert_with("FORCE1", "counting", source, "application/pdf")
        processor.convert_with("FORCE1", "counting", source, "application/pdf")

        assert converter.calls == 2

    def test_stale_hash_is_not_skipped(self, tmp_path):
        cache_dir = tmp_path / "cache"
        converter = CountingConverter(hold_seconds=0.0)
        processor = DocumentProcessor(cache_dir, converters=[converter])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        processor.convert_with("STALE1", "counting", source, "application/pdf")
        source.write_text("changed pdf content", encoding="utf-8")
        processor.convert_with("STALE1", "counting", source, "application/pdf", skip_converted=True)

        assert converter.calls == 2
