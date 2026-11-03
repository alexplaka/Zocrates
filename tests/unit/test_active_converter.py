"""Unit tests for DB schema update, active converter, and convert_with (Phase 3).

Tests the updated DB schema (converter_id, converter_version, parent_item_type,
indexed_at columns), active converter preference on cache miss, priority chain
fallback with logging, convert_with explicit path, and config helper.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from zocrates.rag.document_converters import BaseDocumentConverter
from zocrates.rag.document_processor import (
    DocumentProcessor,
    UnsupportedConversionError,
    _open_meta_db,
)


# ---------------------------------------------------------------------------
# Test converters
# ---------------------------------------------------------------------------

class PdfOnlyConverter(BaseDocumentConverter):
    """Converter that only supports PDFs."""

    @property
    def converter_id(self) -> str:
        return "pdf_only"

    @property
    def converter_version(self) -> str:
        return "2.0.0"

    def supports(self, content_type: str, source_path: Path) -> bool:
        return content_type == "application/pdf"

    def convert(
        self, item_key: str, source_path: Path, content_type: str, cache_dir: Path,
    ) -> str | None:
        return f"# PDF-only conversion of {source_path.name}"


class UniversalConverter(BaseDocumentConverter):
    """Converter that supports everything."""

    @property
    def converter_id(self) -> str:
        return "universal"

    def supports(self, content_type: str, source_path: Path) -> bool:
        return True

    def convert(
        self, item_key: str, source_path: Path, content_type: str, cache_dir: Path,
    ) -> str | None:
        return f"# Universal conversion of {source_path.name}"


class EmptyConverter(BaseDocumentConverter):
    """Converter that always returns None (empty content)."""

    @property
    def converter_id(self) -> str:
        return "empty"

    def supports(self, content_type: str, source_path: Path) -> bool:
        return True

    def convert(
        self, item_key: str, source_path: Path, content_type: str, cache_dir: Path,
    ) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "md_cache"


@pytest.fixture
def txt_file(tmp_path) -> Path:
    f = tmp_path / "doc.txt"
    f.write_text("hello world", encoding="utf-8")
    return f


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    f = tmp_path / "paper.pdf"
    f.write_text("fake pdf content", encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# DB Schema Tests
# ---------------------------------------------------------------------------

class TestDBSchema:
    """Verify the updated DB schema with new columns."""

    def test_schema_has_converter_columns(self, cache_dir, txt_file) -> None:
        """converter_id and converter_version are stored in the DB."""
        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert("K1", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id, converter_version FROM conversion_meta "
                "WHERE item_key = ?",
                ("K1",),
            ).fetchone()

        assert row is not None
        assert row["converter_id"] == "direct_read"
        assert row["converter_version"] == "1.0.0"

    def test_schema_has_indexed_at_column(self, cache_dir, txt_file) -> None:
        """indexed_at column exists and defaults to NULL."""
        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert("K2", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT indexed_at FROM conversion_meta WHERE item_key = ?",
                ("K2",),
            ).fetchone()

        assert row is not None
        assert row["indexed_at"] is None

    def test_schema_has_parent_item_type_column(self, cache_dir, txt_file) -> None:
        """parent_item_type column exists and defaults to NULL."""
        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert("K3", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT parent_item_type FROM conversion_meta WHERE item_key = ?",
                ("K3",),
            ).fetchone()

        assert row is not None
        assert row["parent_item_type"] is None

    def test_converter_id_recorded_for_custom_converter(
        self, cache_dir, txt_file
    ) -> None:
        """A custom converter's ID is recorded in the DB."""
        processor = DocumentProcessor(
            cache_dir, converters=[UniversalConverter()]
        )
        processor.get_or_convert("K4", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id, converter_version FROM conversion_meta "
                "WHERE item_key = ?",
                ("K4",),
            ).fetchone()

        assert row["converter_id"] == "universal"
        assert row["converter_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# Active Converter Tests
# ---------------------------------------------------------------------------

class TestActiveConverter:
    """Verify document_converter_id preference on cache miss."""

    def test_active_converter_used_on_cache_miss(
        self, cache_dir, txt_file
    ) -> None:
        """When document_converter_id is set and supports content type, it's used."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
            document_converter_id="universal",
        )
        processor.get_or_convert("A1", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id FROM conversion_meta WHERE item_key = ?",
                ("A1",),
            ).fetchone()

        assert row["converter_id"] == "universal"

    def test_active_converter_fallback_on_unsupported(
        self, cache_dir, txt_file
    ) -> None:
        """When active converter doesn't support content type, falls back."""
        # pdf_only doesn't support text/plain, so should fall back to universal
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
            document_converter_id="pdf_only",
        )
        processor.get_or_convert("A2", txt_file, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id FROM conversion_meta WHERE item_key = ?",
                ("A2",),
            ).fetchone()

        assert row["converter_id"] == "universal"

    def test_fallback_logs_notification(self, cache_dir, txt_file, caplog) -> None:
        """Fallback from active converter produces a log notification."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
            document_converter_id="pdf_only",
        )

        with caplog.at_level("INFO", logger="zocrates.rag.document_processor"):
            processor.get_or_convert("A3", txt_file, "text/plain")

        assert any("falling back" in r.message.lower() for r in caplog.records)
        assert any("pdf_only" in r.message for r in caplog.records)

    def test_no_active_converter_uses_priority_chain(
        self, cache_dir, pdf_file
    ) -> None:
        """Without document_converter_id, priority chain is used (first match)."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
            document_converter_id=None,
        )
        processor.get_or_convert("A4", pdf_file, "application/pdf")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id FROM conversion_meta WHERE item_key = ?",
                ("A4",),
            ).fetchone()

        # PdfOnlyConverter is first in the list and supports PDFs
        assert row["converter_id"] == "pdf_only"

    def test_cache_hit_ignores_active_converter(
        self, cache_dir, txt_file
    ) -> None:
        """On cache hit (hash match), the active converter is irrelevant."""
        # First conversion with universal
        proc1 = DocumentProcessor(
            cache_dir,
            converters=[UniversalConverter(), PdfOnlyConverter()],
            document_converter_id="universal",
        )
        proc1.get_or_convert("A5", txt_file, "text/plain")

        # Second call with different active -- should still cache-hit
        proc2 = DocumentProcessor(
            cache_dir,
            converters=[UniversalConverter(), PdfOnlyConverter()],
            document_converter_id="pdf_only",
        )
        result = proc2.get_or_convert("A5", txt_file, "text/plain")

        assert result is not None
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id FROM conversion_meta WHERE item_key = ?",
                ("A5",),
            ).fetchone()
        # Still the original converter, not the new active
        assert row["converter_id"] == "universal"


# ---------------------------------------------------------------------------
# convert_with Tests
# ---------------------------------------------------------------------------

class TestConvertWith:
    """Verify explicit conversion via convert_with."""

    def test_convert_with_specific_converter(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with uses the specified converter."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
        )
        result = processor.convert_with(
            "CW1", "universal", txt_file, "text/plain"
        )

        assert "Universal conversion" in result
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id, converter_version FROM conversion_meta "
                "WHERE item_key = ?",
                ("CW1",),
            ).fetchone()
        assert row["converter_id"] == "universal"

    def test_convert_with_raises_on_unsupported(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with raises UnsupportedConversionError for unsupported types."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
        )

        with pytest.raises(UnsupportedConversionError, match="pdf_only"):
            processor.convert_with("CW2", "pdf_only", txt_file, "text/plain")

    def test_convert_with_raises_on_unknown_converter(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with raises KeyError for unregistered converter ID."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[UniversalConverter()],
        )

        with pytest.raises(KeyError, match="nonexistent"):
            processor.convert_with("CW3", "nonexistent", txt_file, "text/plain")

    def test_convert_with_destroys_existing_bundle(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with deletes the old bundle before writing the new one."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
        )

        # Initial conversion
        processor.convert_with("CW4", "universal", txt_file, "text/plain")
        bundle_dir = cache_dir / "CW4"
        # Plant a sentinel file in the bundle to verify rmtree
        sentinel = bundle_dir / "old_artifact.txt"
        sentinel.write_text("should be deleted", encoding="utf-8")

        # Re-convert
        processor.convert_with("CW4", "universal", txt_file, "text/plain")

        assert not sentinel.exists()
        assert (bundle_dir / "document.md").exists()

    def test_convert_with_updates_db_row(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with replaces the DB row with the new converter's identity."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[PdfOnlyConverter(), UniversalConverter()],
        )

        # First: universal
        processor.convert_with("CW5", "universal", txt_file, "text/plain")
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id FROM conversion_meta WHERE item_key = ?",
                ("CW5",),
            ).fetchone()
        assert row["converter_id"] == "universal"

        # Now convert with a PDF converter on a "PDF" (fake)
        processor.convert_with("CW5", "pdf_only", txt_file, "application/pdf")
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT converter_id, converter_version FROM conversion_meta "
                "WHERE item_key = ?",
                ("CW5",),
            ).fetchone()
        assert row["converter_id"] == "pdf_only"
        assert row["converter_version"] == "2.0.0"

    def test_convert_with_raises_on_empty_content(
        self, cache_dir, txt_file
    ) -> None:
        """convert_with raises RuntimeError if the converter returns None."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[EmptyConverter()],
        )

        with pytest.raises(RuntimeError, match="empty content"):
            processor.convert_with("CW6", "empty", txt_file, "text/plain")

    def test_failed_conversion_preserves_existing_bundle(
        self, cache_dir, txt_file
    ) -> None:
        """If conversion fails or returns empty, the existing bundle is preserved."""
        processor = DocumentProcessor(
            cache_dir,
            converters=[UniversalConverter(), EmptyConverter()],
        )

        # 1. Initial successful conversion
        processor.convert_with("CW7", "universal", txt_file, "text/plain")
        old_content = processor.read_cached("CW7")
        assert old_content is not None

        # 2. Attempt failed conversion with EmptyConverter
        with pytest.raises(RuntimeError, match="empty content"):
            processor.convert_with("CW7", "empty", txt_file, "text/plain")

        # 3. Verify original bundle is still intact
        assert processor.read_cached("CW7") == old_content


# ---------------------------------------------------------------------------
# Config Helper Tests
# ---------------------------------------------------------------------------

class TestConfigHelper:
    """Verify get_document_converter_id and get_document_converter_mode config helpers."""

    def test_returns_defaults_when_not_configured(self, tmp_path) -> None:
        from zocrates.config import (
            get_document_converter_id,
            get_document_converter_mode,
        )

        # No config file, no env var
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("DOCUMENT_CONVERTER_ID", None)
            os.environ.pop("DOCUMENT_CONVERTER_MODE", None)
            assert get_document_converter_id(tmp_path) == "markitdown"
            assert get_document_converter_mode(tmp_path) == "local"

    def test_reads_from_config_file(self, tmp_path) -> None:
        import json
        from zocrates.config import (
            get_document_converter_id,
            get_document_converter_mode,
            get_config_path,
        )

        config_path = get_config_path(tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({
                "document_converter_id": "docling_local",
                "document_converter_mode": "local",
            }),
            encoding="utf-8",
        )

        import os
        os.environ.pop("DOCUMENT_CONVERTER_ID", None)
        os.environ.pop("DOCUMENT_CONVERTER_MODE", None)
        assert get_document_converter_id(tmp_path) == "docling_local"
        assert get_document_converter_mode(tmp_path) == "local"

    def test_env_var_precedence(self, tmp_path) -> None:
        import json
        from zocrates.config import (
            get_document_converter_id,
            get_document_converter_mode,
            get_config_path,
        )

        config_path = get_config_path(tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({
                "document_converter_id": "docling",
                "document_converter_mode": "local",
            }),
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {
                "DOCUMENT_CONVERTER_ID": "chandra_ocr",
                "DOCUMENT_CONVERTER_MODE": "cloud",
            },
        ):
            assert get_document_converter_id(tmp_path) == "chandra_ocr"
            assert get_document_converter_mode(tmp_path) == "cloud"

    def test_datalab_non_pdf_fallback_config(self, tmp_path) -> None:
        import json
        from zocrates.config import (
            get_datalab_non_pdf_fallback,
            get_config_path,
        )

        # Default fallback
        assert get_datalab_non_pdf_fallback(tmp_path) == "markitdown"

        # Config file override
        config_path = get_config_path(tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"datalab_non_pdf_fallback": "docling_local"}),
            encoding="utf-8",
        )
        assert get_datalab_non_pdf_fallback(tmp_path) == "docling_local"

        # Env var precedence
        with patch.dict("os.environ", {"DATALAB_NON_PDF_FALLBACK": "markitdown"}):
            assert get_datalab_non_pdf_fallback(tmp_path) == "markitdown"


# ---------------------------------------------------------------------------
# Datalab Fallback Resolution Tests
# ---------------------------------------------------------------------------

class TestDatalabNonPdfFallback:
    """Verify DocumentProcessor auto-dispatch fallback for Datalab converter."""

    def test_datalab_active_pdf_uses_datalab(self, cache_dir, pdf_file) -> None:
        """When datalab is active and file is PDF, uses datalab."""
        class FakeDatalab(PdfOnlyConverter):
            @property
            def converter_id(self) -> str:
                return "datalab"

        class FakeMarkItDown(UniversalConverter):
            @property
            def converter_id(self) -> str:
                return "markitdown"

        processor = DocumentProcessor(
            cache_dir,
            converters=[FakeDatalab(), FakeMarkItDown()],
            document_converter_id="datalab",
            datalab_non_pdf_fallback="markitdown",
        )

        resolved = processor._resolve_converter("application/pdf", pdf_file)
        assert resolved is not None
        assert resolved.converter_id == "datalab"

    def test_datalab_active_html_uses_configured_fallback(self, cache_dir, txt_file) -> None:
        """When datalab is active and file is non-PDF (HTML/TXT), routes to configured fallback."""
        pdf_conv = PdfOnlyConverter()
        # Mock datalab ID
        class FakeDatalab(PdfOnlyConverter):
            @property
            def converter_id(self) -> str:
                return "datalab"

        class FakeMarkItDown(UniversalConverter):
            @property
            def converter_id(self) -> str:
                return "markitdown"

        class FakeDocling(UniversalConverter):
            @property
            def converter_id(self) -> str:
                return "docling_local"

        processor = DocumentProcessor(
            cache_dir,
            converters=[FakeDatalab(), FakeMarkItDown(), FakeDocling()],
            document_converter_id="datalab",
            datalab_non_pdf_fallback="docling_local",
        )

        resolved = processor._resolve_converter("text/html", txt_file)
        assert resolved is not None
        assert resolved.converter_id == "docling_local"

        # Dynamically change fallback to markitdown
        processor.datalab_non_pdf_fallback = "markitdown"
        resolved2 = processor._resolve_converter("text/html", txt_file)
        assert resolved2 is not None
        assert resolved2.converter_id == "markitdown"

    def test_convert_with_datalab_non_pdf_routes_to_fallback(self, cache_dir, txt_file) -> None:
        """Explicit convert_with('datalab') routes non-PDFs to configured fallback."""
        class FakeDatalab(PdfOnlyConverter):
            @property
            def converter_id(self) -> str:
                return "datalab"

        class FakeMarkItDown(UniversalConverter):
            @property
            def converter_id(self) -> str:
                return "markitdown"

        processor = DocumentProcessor(
            cache_dir,
            converters=[FakeDatalab(), FakeMarkItDown()],
            datalab_non_pdf_fallback="markitdown",
        )

        res = processor.convert_with("CW_FALLBACK", "datalab", txt_file, "text/html")
        assert "Universal conversion" in res
        meta = processor.get_metadata("CW_FALLBACK")
        assert meta is not None
        assert meta["converter_id"] == "markitdown"

