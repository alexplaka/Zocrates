"""Unit tests for the converter identity contract (Phase 1).

Tests that BaseDocumentConverter enforces converter_id as abstract,
provides a default converter_version, and that concrete converters
expose correct identity values.
"""

from pathlib import Path

import pytest

from zocrates.rag.document_converters import (
    BaseDocumentConverter,
    DatalabConverter,
    DirectReadConverter,
    MarkItDownConverter,
)


class TestConverterIdentityContract:
    """Verify the abstract identity contract on BaseDocumentConverter."""

    def test_missing_converter_id_raises_type_error(self) -> None:
        """A subclass that omits converter_id cannot be instantiated."""

        class IncompleteConverter(BaseDocumentConverter):
            def supports(self, content_type: str, source_path: Path) -> bool:
                return True

            def convert(
                self,
                item_key: str,
                source_path: Path,
                content_type: str,
                cache_dir: Path,
            ) -> str | None:
                return "text"

        with pytest.raises(TypeError, match="converter_id"):
            IncompleteConverter()

    def test_default_converter_version(self) -> None:
        """A subclass that only implements converter_id gets the default version."""

        class MinimalConverter(BaseDocumentConverter):
            @property
            def converter_id(self) -> str:
                return "minimal"

            def supports(self, content_type: str, source_path: Path) -> bool:
                return True

            def convert(
                self,
                item_key: str,
                source_path: Path,
                content_type: str,
                cache_dir: Path,
            ) -> str | None:
                return "text"

        converter = MinimalConverter()
        assert converter.converter_version == "1.0.0"

    def test_custom_converter_version_override(self) -> None:
        """A subclass can override converter_version."""

        class VersionedConverter(BaseDocumentConverter):
            @property
            def converter_id(self) -> str:
                return "versioned"

            @property
            def converter_version(self) -> str:
                return "2.5.0"

            def supports(self, content_type: str, source_path: Path) -> bool:
                return True

            def convert(
                self,
                item_key: str,
                source_path: Path,
                content_type: str,
                cache_dir: Path,
            ) -> str | None:
                return "text"

        converter = VersionedConverter()
        assert converter.converter_version == "2.5.0"


class TestDirectReadConverterIdentity:
    """Verify DirectReadConverter identity values."""

    def test_converter_id(self) -> None:
        converter = DirectReadConverter()
        assert converter.converter_id == "direct_read"

    def test_converter_version(self) -> None:
        converter = DirectReadConverter()
        assert converter.converter_version == "1.0.0"


class TestMarkItDownConverterIdentity:
    """Verify MarkItDownConverter identity values."""

    def test_converter_id(self) -> None:
        converter = MarkItDownConverter()
        assert converter.converter_id == "markitdown"

    def test_converter_version(self) -> None:
        import importlib.metadata
        converter = MarkItDownConverter()
        expected = importlib.metadata.version("markitdown")
        assert converter.converter_version == expected


class TestDatalabConverterIdentityContract:
    """Verify DatalabConverter identity values."""

    def test_converter_id(self) -> None:
        converter = DatalabConverter()
        assert converter.converter_id == "datalab"

    def test_converter_is_remote(self) -> None:
        converter = DatalabConverter()
        assert converter.is_remote is True
