"""Unit tests for Datalab processing-mode resolution and per-item conversion locking.

Covers the plan §3.6 resolver truth table (config helper, shared validation
helper, and ``DocumentProcessor`` resolution for both the ``convert_with`` and
``get_or_convert`` paths) plus the plan §3.4 per-item conversion lock.
"""

import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from zocrates.config import (
    DATALAB_PROCESSING_MODES,
    DEFAULT_DATALAB_PROCESSING_MODE,
    get_datalab_processing_mode,
    validate_datalab_mode,
)
from zocrates.rag.document_converters import BaseDocumentConverter, DatalabConverter
from zocrates.rag.document_processor import DocumentProcessor

RESOLVER_LOGGER = "zocrates.rag.document_processor"


class RecordingDatalabConverter(DatalabConverter):
    """DatalabConverter that records the options it receives instead of calling the API."""

    def __init__(self) -> None:
        super().__init__()
        self.received_options: list[dict[str, Any] | None] = []

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
        options: dict[str, Any] | None = None,
    ) -> str | None:
        self.received_options.append(options)
        return f"# Datalab-mode recording conversion of {source_path.name}"

class RecordingCustomConverter(BaseDocumentConverter):
    """Non-Datalab converter that records the options it receives."""

    def __init__(self) -> None:
        self.received_options: list[dict[str, Any] | None] = []

    @property
    def converter_id(self) -> str:
        return "recording_custom"

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
        self.received_options.append(options)
        return "# Recording custom conversion"


@pytest.fixture
def pdf_path(tmp_path) -> Path:
    """Provide a minimal PDF-ish source file."""
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4 test bytes")
    return path


# ---------------------------------------------------------------------------
# Shared validation helper + config getter
# ---------------------------------------------------------------------------
class TestValidateDatalabMode:
    """Test the shared {fast, balanced, accurate} validation helper."""

    @pytest.mark.parametrize("mode", ["fast", "balanced", "accurate"])
    def test_supported_modes_pass_through(self, mode):
        assert validate_datalab_mode(mode) == mode

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(" Fast ", "fast"), ("ACCURATE", "accurate"), ("balanced\n", "balanced")],
    )
    def test_values_are_normalized(self, raw, expected):
        assert validate_datalab_mode(raw) == expected

    @pytest.mark.parametrize("raw", ["lite", "", "   ", "garbage", None, 3, ["fast"]])
    def test_unsupported_values_are_rejected(self, raw):
        assert validate_datalab_mode(raw) is None

    def test_lite_is_never_a_supported_mode(self):
        assert "lite" not in DATALAB_PROCESSING_MODES


class TestGetDatalabProcessingMode:
    """Test the Phase 2 config getter (Phase 1: absent key -> 'balanced')."""

    def test_absent_key_returns_balanced(self):
        assert get_datalab_processing_mode(cfg={}) == "balanced"
        assert get_datalab_processing_mode(cfg={"llm_model": "x"}) == "balanced"

    def test_default_constant_is_balanced(self):
        assert DEFAULT_DATALAB_PROCESSING_MODE == "balanced"

    def test_valid_config_value_is_revalidated_and_honored(self):
        assert get_datalab_processing_mode(cfg={"datalab_processing_mode": "fast"}) == "fast"

    def test_invalid_config_value_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING, logger="zocrates.config"):
            mode = get_datalab_processing_mode(cfg={"datalab_processing_mode": "lite"})

        assert mode == "balanced"
        assert "datalab_processing_mode" in caplog.text

    def test_cfg_none_loads_config_file(self):
        with patch("zocrates.config.load_zocrates_config", return_value={}) as loader:
            assert get_datalab_processing_mode(Path("/nonexistent/zotero")) == "balanced"
        loader.assert_called_once_with(Path("/nonexistent/zotero"))


# ---------------------------------------------------------------------------
# DocumentProcessor resolution (§3.6 truth table)
# ---------------------------------------------------------------------------
class TestDatalabModeResolution:
    """Test mode resolution for both the convert_with and get_or_convert paths."""

    def _processor(self, cache_dir: Path, converter: BaseDocumentConverter) -> DocumentProcessor:
        return DocumentProcessor(cache_dir, converters=[converter])

    @pytest.mark.parametrize("mode", ["fast", "balanced", "accurate"])
    def test_explicit_valid_mode_wins(self, tmp_path, pdf_path, mode):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)

        processor.convert_with("KEY", "datalab", pdf_path, "application/pdf", options={"mode": mode})

        assert conv.received_options == [{"mode": mode}]

    @pytest.mark.parametrize("options", [None, {}, {"mode": None}, {"mode": ""}, {"mode": "   "}])
    def test_missing_mode_uses_config_default(self, tmp_path, pdf_path, options, caplog):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)

        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode", return_value="balanced"
        ):
            processor.convert_with(
                "KEY", "datalab", pdf_path, "application/pdf", options=options
            )

        assert conv.received_options[0]["mode"] == "balanced"
        # Missing/None/blank modes are "unprovided" — no invalid-value warning (§3.6 truth table)
        assert not any("Invalid Datalab processing mode" in r.message for r in caplog.records)

    def test_invalid_explicit_mode_warns_and_uses_default(self, tmp_path, pdf_path, caplog):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)

        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode", return_value="balanced"
        ):
            processor.convert_with(
                "KEY", "datalab", pdf_path, "application/pdf", options={"mode": "lite"}
            )

        assert conv.received_options[0]["mode"] == "balanced"
        assert any("Invalid Datalab processing mode" in r.message for r in caplog.records)

    def test_config_default_is_plumbed_not_hardcoded(self, tmp_path, pdf_path):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)

        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode", return_value="accurate"
        ):
            processor.convert_with("KEY", "datalab", pdf_path, "application/pdf")

        assert conv.received_options == [{"mode": "accurate"}]

    def test_invalid_explicit_mode_warns_and_uses_config_default(self, tmp_path, pdf_path, caplog):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)

        with (
            patch(
                "zocrates.rag.document_processor.get_datalab_processing_mode",
                return_value="balanced",
            ),
            caplog.at_level(logging.WARNING, logger=RESOLVER_LOGGER),
        ):
            processor.convert_with(
                "KEY", "datalab", pdf_path, "application/pdf", options={"mode": "lite"}
            )

        assert conv.received_options == [{"mode": "balanced"}]
        assert "lite" in caplog.text

    def test_other_option_keys_are_preserved_and_caller_dict_not_mutated(self, tmp_path, pdf_path):
        conv = RecordingDatalabConverter()
        processor = self._processor(tmp_path / "cache", conv)
        caller_options = {"mode": "fast", "unrelated": 7}

        processor.convert_with(
            "KEY", "datalab", pdf_path, "application/pdf", options=caller_options
        )

        assert conv.received_options == [{"mode": "fast", "unrelated": 7}]
        assert caller_options == {"mode": "fast", "unrelated": 7}

    def test_non_datalab_converter_receives_options_untouched(self, tmp_path):
        conv = RecordingCustomConverter()
        processor = self._processor(tmp_path / "cache", conv)
        source = tmp_path / "note.txt"
        source.write_text("plain text", encoding="utf-8")

        processor.convert_with(
            "KEY", "recording_custom", source, "text/plain", options={"mode": "fast"}
        )

        # No Datalab mode injection: the dict reaches the converter verbatim.
        assert conv.received_options == [{"mode": "fast"}]

    def test_non_datalab_converter_receives_none_options(self, tmp_path):
        conv = RecordingCustomConverter()
        processor = self._processor(tmp_path / "cache", conv)
        source = tmp_path / "note.txt"
        source.write_text("plain text", encoding="utf-8")

        processor.convert_with("KEY", "recording_custom", source, "text/plain")

        assert conv.received_options == [None]

    def test_get_or_convert_resolves_config_default_for_datalab(self, tmp_path, pdf_path):
        conv = RecordingDatalabConverter()
        processor = DocumentProcessor(
            tmp_path / "cache", converters=[conv], document_converter_id="datalab"
        )

        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode", return_value="fast"
        ):
            markdown = processor.get_or_convert("KEY", pdf_path, "application/pdf")

        assert markdown is not None
        assert conv.received_options == [{"mode": "fast"}]

    def test_get_or_convert_cache_hit_ignores_changed_global_default(self, tmp_path, pdf_path):
        """Changing the global default must never re-convert unchanged items (plan §3.7)."""
        conv = RecordingDatalabConverter()
        processor = DocumentProcessor(
            tmp_path / "cache", converters=[conv], document_converter_id="datalab"
        )

        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode", return_value="fast"
        ):
            first = processor.get_or_convert("KEY", pdf_path, "application/pdf")
        assert first is not None
        assert conv.received_options == [{"mode": "fast"}]

        # The default changes to an expensive tier: the hash-only cache path still
        # returns the existing bundle without converting again.
        with patch(
            "zocrates.rag.document_processor.get_datalab_processing_mode",
            return_value="accurate",
        ):
            second = processor.get_or_convert("KEY", pdf_path, "application/pdf")

        assert second == first
        assert conv.received_options == [{"mode": "fast"}], (
            "an unchanged cached item must stay a cache hit after a default change"
        )


# ---------------------------------------------------------------------------
# Per-item conversion lock (§3.4)
# ---------------------------------------------------------------------------
class SerializationProbeConverter(BaseDocumentConverter):
    """Converter that records how many conversions of one item run concurrently."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    @property
    def converter_id(self) -> str:
        return "serialization_probe"

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
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            call_number = self.calls
        try:
            time.sleep(self.hold_seconds)
            return f"# Conversion #{call_number} of {source_path.name}"
        finally:
            with self._guard:
                self.active -= 1


class TestPerItemConversionLock:
    """Overlapping conversions of the same item must be serialized."""

    def test_convert_with_serializes_same_item(self, tmp_path):
        cache_dir = tmp_path / "cache"
        conv = SerializationProbeConverter()
        processor = DocumentProcessor(cache_dir, converters=[conv])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(
                    processor.convert_with("LOCK1", "serialization_probe", source, "application/pdf")
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(results) == 2
        assert conv.calls == 2
        assert conv.max_active == 1, "per-item lock must serialize the conversion window"

        # The surviving bundle is internally consistent: the staged directory was
        # swapped in atomically and the metadata row matches the final document.
        item_dir = cache_dir / "LOCK1"
        assert item_dir.is_dir()
        assert not (cache_dir / ".tmp_LOCK1").exists()
        document_md = (item_dir / "document.md").read_text(encoding="utf-8")
        meta = processor.get_metadata("LOCK1")
        assert meta is not None
        assert meta["converter_id"] == "serialization_probe"
        assert meta["char_count"] == len(document_md)
        assert any(
            f"# Conversion #{n} of paper.pdf" in document_md for n in (1, 2)
        )

    def test_get_or_convert_cache_recheck_suppresses_duplicate_conversion(self, tmp_path):
        cache_dir = tmp_path / "cache"
        conv = SerializationProbeConverter()
        processor = DocumentProcessor(cache_dir, converters=[conv])
        source = tmp_path / "paper.pdf"
        source.write_text("pdf content", encoding="utf-8")

        barrier = threading.Barrier(2)
        results: list[str | None] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(processor.get_or_convert("LOCK2", source, "application/pdf"))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        # The second caller re-checks the cache after acquiring the lock and
        # returns the cached bundle instead of paying for a duplicate conversion.
        assert conv.calls == 1
        assert all(result is not None for result in results)
