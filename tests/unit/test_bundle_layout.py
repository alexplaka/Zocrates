"""Unit tests for item-centric bundle directory structure (Phase 2).

Tests that DocumentProcessor uses the <item_key>/document.md layout,
that MarkItDownConverter writes images to <item_key>/images/,
and that read_cached, is_cached, and list_cached_keys work with bundles.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.rag.document_processor import DocumentProcessor


@pytest.fixture
def temp_cache_dir(tmp_path) -> Path:
    """Provide a temporary directory path for the Markdown cache under tmp_path."""
    return tmp_path / "md_cache"


class TestBundleDirectoryLayout:
    """Verify the item-centric bundle directory structure."""

    def test_save_creates_bundle_directory(self, temp_cache_dir, tmp_path) -> None:
        """Conversion creates <item_key>/document.md, not <item_key>.md."""
        source = tmp_path / "notes.md"
        source.write_text("# Hello", encoding="utf-8")

        processor = DocumentProcessor(temp_cache_dir)
        processor.get_or_convert("KEY1", source, "text/markdown")

        assert (temp_cache_dir / "KEY1" / "document.md").exists()
        assert not (temp_cache_dir / "KEY1.md").exists()

    def test_bundle_content_matches(self, temp_cache_dir, tmp_path) -> None:
        """The written document.md contains the converted Markdown."""
        source = tmp_path / "readme.txt"
        source.write_text("plain text content", encoding="utf-8")

        processor = DocumentProcessor(temp_cache_dir)
        processor.get_or_convert("KEY2", source, "text/plain")

        content = (temp_cache_dir / "KEY2" / "document.md").read_text(encoding="utf-8")
        assert "plain text content" in content
        assert "item_key: KEY2" in content

    def test_multiple_items_create_separate_bundles(
        self, temp_cache_dir, tmp_path
    ) -> None:
        """Each item gets its own subdirectory."""
        for key in ("AAA", "BBB", "CCC"):
            source = tmp_path / f"{key}.txt"
            source.write_text(f"content for {key}", encoding="utf-8")
            processor = DocumentProcessor(temp_cache_dir)
            processor.get_or_convert(key, source, "text/plain")

        for key in ("AAA", "BBB", "CCC"):
            assert (temp_cache_dir / key / "document.md").exists()


class TestReadCached:
    """Verify read_cached reads from the bundle layout."""

    def test_read_existing(self, temp_cache_dir, tmp_path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("cached text", encoding="utf-8")

        processor = DocumentProcessor(temp_cache_dir)
        processor.get_or_convert("RKEY", source, "text/plain")

        cached_text = processor.read_cached("RKEY")
        assert cached_text is not None
        assert "cached text" in cached_text
        assert "item_key: RKEY" in cached_text

    def test_read_missing_returns_none(self, temp_cache_dir) -> None:
        processor = DocumentProcessor(temp_cache_dir)
        assert processor.read_cached("NONEXISTENT") is None


class TestIsCached:
    """Verify is_cached checks bundle layout."""

    def test_cached_item(self, temp_cache_dir, tmp_path) -> None:
        source = tmp_path / "file.txt"
        source.write_text("data", encoding="utf-8")

        processor = DocumentProcessor(temp_cache_dir)
        processor.get_or_convert("CKEY", source, "text/plain")

        assert processor.is_cached("CKEY") is True

    def test_uncached_item(self, temp_cache_dir) -> None:
        processor = DocumentProcessor(temp_cache_dir)
        assert processor.is_cached("MISSING") is False

    def test_directory_without_document_md(self, temp_cache_dir) -> None:
        """A bare directory without document.md is not considered cached."""
        (temp_cache_dir / "EMPTY_DIR").mkdir(parents=True)
        processor = DocumentProcessor(temp_cache_dir)
        assert processor.is_cached("EMPTY_DIR") is False


class TestListCachedKeys:
    """Verify list_cached_keys scans bundle directories."""

    def test_lists_converted_items(self, temp_cache_dir, tmp_path) -> None:
        processor = DocumentProcessor(temp_cache_dir)
        for key in ("X1", "X2", "X3"):
            source = tmp_path / f"{key}.txt"
            source.write_text(f"content {key}", encoding="utf-8")
            processor.get_or_convert(key, source, "text/plain")

        keys = sorted(processor.list_cached_keys())
        assert keys == ["X1", "X2", "X3"]

    def test_ignores_non_bundle_files(self, temp_cache_dir, tmp_path) -> None:
        """Files like conversion_meta.db or stray .md files are not listed."""
        processor = DocumentProcessor(temp_cache_dir)
        source = tmp_path / "real.txt"
        source.write_text("content", encoding="utf-8")
        processor.get_or_convert("REAL", source, "text/plain")

        # Create a stray file that should not be listed
        (temp_cache_dir / "stray.md").write_text("noise", encoding="utf-8")

        keys = processor.list_cached_keys()
        assert keys == ["REAL"]

    def test_empty_cache(self, temp_cache_dir) -> None:
        processor = DocumentProcessor(temp_cache_dir)
        assert processor.list_cached_keys() == []


class TestImageBundleLayout:
    """Verify MarkItDownConverter writes images into <item_key>/images/."""

    def test_image_dir_inside_item_bundle(self, temp_cache_dir, tmp_path) -> None:
        """Images are extracted into <item_key>/images/, not images/<item_key>/."""
        source = tmp_path / "paper.pdf"
        source.write_text("fake pdf", encoding="utf-8")

        with patch("zocrates.rag.document_converters.MarkItDown") as mock_mid_cls:
            mock_mid = MagicMock()
            mock_result = MagicMock()
            mock_result.text_content = "# Paper Content"
            mock_mid.convert.return_value = mock_result
            mock_mid_cls.return_value = mock_mid

            with patch(
                "zocrates.rag.document_converters._extract_pdf_images"
            ) as mock_extract:
                # Simulate image extraction writing to the images dir
                def side_effect(pdf_path, output_dir):
                    output_dir.mkdir(parents=True, exist_ok=True)
                    img = output_dir / "img-000.png"
                    img.write_bytes(b"fake png")
                    return {1: [img]}

                mock_extract.side_effect = side_effect

                processor = DocumentProcessor(temp_cache_dir)
                processor.get_or_convert("IMGKEY", source, "application/pdf")

                # Verify the extract call received the item-level images dir
                call_args = mock_extract.call_args
                images_dir_arg = call_args[0][1]
                assert images_dir_arg == temp_cache_dir / "IMGKEY" / "images"

                # Verify the image file exists inside the bundle
                assert (temp_cache_dir / "IMGKEY" / "images" / "img-000.png").exists()

                # Verify nothing exists at the old layout
                assert not (temp_cache_dir / "images").exists()


class TestConverterReceivesItemDir:
    """Verify _convert passes the item-level directory to converters."""

    def test_converter_gets_item_dir(self, temp_cache_dir, tmp_path) -> None:
        """The converter's cache_dir argument is corpus/<item_key>/."""
        source = tmp_path / "doc.txt"
        source.write_text("text", encoding="utf-8")

        from zocrates.rag.document_converters import BaseDocumentConverter

        received_dirs: list[Path] = []

        class SpyConverter(BaseDocumentConverter):
            @property
            def converter_id(self) -> str:
                return "spy"

            def supports(self, content_type: str, source_path: Path) -> bool:
                return True

            def convert(
                self,
                item_key: str,
                source_path: Path,
                content_type: str,
                cache_dir: Path,
            ) -> str | None:
                received_dirs.append(cache_dir)
                return "spied"

        processor = DocumentProcessor(temp_cache_dir, converters=[SpyConverter()])
        processor.get_or_convert("SPYKEY", source, "text/plain")

        assert len(received_dirs) == 1
        assert received_dirs[0] == temp_cache_dir / "SPYKEY"


class TestAssetCatalogInBundle:
    """Verify that both images and tables in an item bundle are cataloged in index.md."""

    def test_bundle_catalogs_both_images_and_tables(
        self, temp_cache_dir, tmp_path
    ) -> None:
        source = tmp_path / "paper.txt"
        source.write_text("content with figures and tables", encoding="utf-8")

        processor = DocumentProcessor(temp_cache_dir)
        processor.get_or_convert("MULTI_ASSET", source, "text/plain", title="Multi Asset Paper")

        bundle_dir = temp_cache_dir / "MULTI_ASSET"
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "figure_001.png").write_bytes(b"png")

        tables_dir = bundle_dir / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        (tables_dir / "table_001.csv").write_text("col1,col2\nval1,val2", encoding="utf-8")

        # Re-generate item catalog with the new assets
        from zocrates.rag.bundle_catalog import generate_item_index_md
        generate_item_index_md(
            item_dir=bundle_dir,
            item_key="MULTI_ASSET",
            title="Multi Asset Paper",
            converter_id="direct_read",
            converter_version="1.0.0",
        )

        catalog_text = (bundle_dir / "index.md").read_text(encoding="utf-8")
        assert "### Images" in catalog_text
        assert "[images/figure_001.png](./images/figure_001.png)" in catalog_text
        assert "### Tables" in catalog_text
        assert "[tables/table_001.csv](./tables/table_001.csv)" in catalog_text


class TestAgentsMdAndRelationsLayout:
    """Verify AGENTS.md guide and item_relations.md layout (Phase 2)."""

    def test_generate_agents_md_includes_relations_guide(self, temp_cache_dir) -> None:
        from zocrates.rag.bundle_catalog import generate_agents_md

        temp_cache_dir.mkdir(parents=True, exist_ok=True)
        generate_agents_md(temp_cache_dir)

        agents_md = temp_cache_dir / "AGENTS.md"
        assert agents_md.exists()
        text = agents_md.read_text(encoding="utf-8")

        # 1. Directory Structure contains item_relations.md
        assert "item_relations.md         # Related literature neighborhood" in text

        # 2. Agent Navigation Protocol contains Step 5
        assert "### Step 5: Relation & Literature Graph Traversal" in text
        assert "Check `<item_key>/item_relations.md`" in text
        assert "If 0 bytes, the paper has no corroborated relations in the library (unconnected paper)." in text

        # 3. Strict Guardrails document item_relations.md safety
        assert "item_relations.md" in text
        assert "0-byte orphan `item_relations.md` files are intentional" in text

    def test_generate_item_relations_md_linked_and_unlinked(self, temp_cache_dir) -> None:
        from zocrates.rag.bundle_catalog import generate_item_relations_md

        temp_cache_dir.mkdir(parents=True, exist_ok=True)
        # Create converted bundle for TARGET_CONV
        target_dir = temp_cache_dir / "TARGET_CONV"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "document.md").write_text("# Target Converted Paper\n", encoding="utf-8")

        relations = [
            {"target_key": "TARGET_UNCONV", "score": 0.85, "relation_type": "hybrid_search"},
            {"target_key": "TARGET_CONV", "score": 0.95, "relation_type": "human_curated"},
        ]
        titles = {
            "TARGET_CONV": "Converted Target Paper",
            "TARGET_UNCONV": "Unconverted Target Paper",
        }

        rendered = generate_item_relations_md("SRC1", relations, titles, cache_dir=temp_cache_dir)
        lines = rendered.splitlines()

        # Converted bundle produces markdown link
        assert "- [TARGET_CONV - Converted Target Paper](../TARGET_CONV/document.md)" in lines
        assert "  - Relation: `human_curated` (score: 0.95)" in lines

        # Unconverted bundle produces unlinked bold reference
        assert "- **[TARGET_UNCONV] Unconverted Target Paper**" in lines
        assert "  - Relation: `hybrid_search` (score: 0.85)" in lines

