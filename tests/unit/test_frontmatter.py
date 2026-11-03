"""Unit tests for frontmatter and catalog index.md generation (Phase 4).

Tests:
  - build_frontmatter generates valid OKF-0.2 YAML blocks with collections.
  - strip_existing_frontmatter strips existing frontmatter blocks correctly.
  - generate_item_index_md creates item-level index.md catalogs.
  - generate_root_index_md creates root-level index.md from conversion_meta.db.
  - DocumentProcessor integration writes document.md with frontmatter and catalogs.
"""

from pathlib import Path

import pytest
import yaml

from zocrates.rag.document_processor import DocumentProcessor, _open_meta_db
from zocrates.rag.bundle_catalog import (
    _compute_relative_source_path,
    build_frontmatter,
    generate_agents_md,
    generate_item_index_md,
    generate_root_index_md,
    strip_existing_frontmatter,
)
from zocrates.rag.zotero_db import ZoteroCollection


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "md_cache"


class TestFrontmatterFormatting:
    """Verify build_frontmatter and strip_existing_frontmatter functions."""

    def test_build_frontmatter_structure(self, tmp_path) -> None:
        collections = [
            ZoteroCollection(name="Transformers", key="CKEY1"),
            {"name": "Attention", "collection_key": "CKEY2"},
        ]
        source_path = tmp_path / "paper.pdf"

        fm_str = build_frontmatter(
            item_key="ITEM123",
            parent_item_type="journalArticle",
            title="Attention Is All You Need",
            collections=collections,
            source_path=source_path,
            source_hash="sha256hash123",
            content_type="application/pdf",
            converter_id="markitdown",
            converter_version="1.0.0",
            converted_at="2026-03-30T12:00:00Z",
        )

        assert fm_str.startswith("---\n")
        assert fm_str.endswith("---\n")

        # Parse YAML block inside --- delimiters
        yaml_content = fm_str.strip("- \n\r")
        parsed = yaml.safe_load(yaml_content)

        assert parsed["type"] == "journalArticle"
        assert parsed["okf_version"] == "0.2"
        assert parsed["item_key"] == "ITEM123"
        assert parsed["parent_item_key"] is None
        assert "parent_item_key: null" in fm_str
        assert parsed["title"] == "Attention Is All You Need"
        assert parsed["member_of_collections"] == [
            {"name": "Transformers", "collection_key": "CKEY1"},
            {"name": "Attention", "collection_key": "CKEY2"},
        ]
        assert parsed["source"]["path"] == str(source_path)
        assert parsed["source"]["hash"] == "sha256hash123"
        assert parsed["source"]["content_type"] == "application/pdf"
        assert parsed["provenance"]["converter_id"] == "markitdown"
        assert parsed["provenance"]["converter_version"] == "1.0.0"
        assert parsed["provenance"]["converted_at"] == "2026-03-30T12:00:00Z"

        # When bundle_dir is provided, source.path is computed as relative POSIX path
        bundle_dir = tmp_path / "cache" / "ITEM123"
        fm_str_rel = build_frontmatter(
            item_key="ITEM123",
            parent_item_type="journalArticle",
            title="Attention Is All You Need",
            collections=collections,
            source_path=source_path,
            source_hash="sha256hash123",
            content_type="application/pdf",
            converter_id="markitdown",
            converter_version="1.0.0",
            converted_at="2026-03-30T12:00:00Z",
            bundle_dir=bundle_dir,
        )
        parsed_rel = yaml.safe_load(fm_str_rel.strip("- \n\r"))
        assert parsed_rel["source"]["path"] == "../../paper.pdf"

    def test_build_frontmatter_with_parent_item_key(self, tmp_path) -> None:
        source_path = tmp_path / "supplement.pdf"
        fm_str = build_frontmatter(
            item_key="ATT456",
            parent_item_type="journalArticle",
            title="Supplementary Material",
            collections=[],
            source_path=source_path,
            source_hash="hash456",
            content_type="application/pdf",
            converter_id="docling_local",
            converter_version="2.0.0",
            converted_at="2026-03-30T12:00:00Z",
            parent_item_key="PARENT123",
        )

        yaml_content = fm_str.strip("- \n\r")
        parsed = yaml.safe_load(yaml_content)
        assert parsed["item_key"] == "ATT456"
        assert parsed["parent_item_key"] == "PARENT123"

    def test_build_frontmatter_with_bibliographic_metadata(self, tmp_path) -> None:
        """Verify that authors, year, publication, doi, and url are placed correctly after title."""
        source_path = tmp_path / "paper.pdf"
        fm_str = build_frontmatter(
            item_key="ATT789",
            parent_item_type="journalArticle",
            title="Attention Is All You Need",
            collections=[],
            source_path=source_path,
            source_hash="hash789",
            content_type="application/pdf",
            converter_id="markitdown",
            converter_version="1.0.0",
            converted_at="2026-03-30T12:00:00Z",
            authors=["Vaswani, Ashish", "Shazeer, Noam", "Parmar, Niki"],
            year=2017,
            publication="Advances in Neural Information Processing Systems",
            doi="10.48550/arXiv.1706.03762",
            url="https://arxiv.org/abs/1706.03762",
        )

        yaml_content = fm_str.strip("- \n\r")
        parsed = yaml.safe_load(yaml_content)

        assert parsed["title"] == "Attention Is All You Need"
        assert parsed["authors"] == ["Vaswani, Ashish", "Shazeer, Noam", "Parmar, Niki"]
        assert parsed["year"] == 2017
        assert parsed["publication"] == "Advances in Neural Information Processing Systems"
        assert parsed["doi"] == "10.48550/arXiv.1706.03762"
        assert parsed["url"] == "https://arxiv.org/abs/1706.03762"

        # Check key order in raw string
        lines = [line.split(":")[0].strip() for line in yaml_content.splitlines() if ":" in line and not line.startswith(" ")]
        assert "title" in lines
        assert "authors" in lines
        assert "year" in lines
        assert "publication" in lines
        assert "doi" in lines
        assert "url" in lines
        title_idx = lines.index("title")
        authors_idx = lines.index("authors")
        year_idx = lines.index("year")
        pub_idx = lines.index("publication")
        doi_idx = lines.index("doi")
        url_idx = lines.index("url")
        assert title_idx < authors_idx < year_idx < pub_idx < doi_idx < url_idx

    def test_build_frontmatter_omits_empty_and_none_metadata(self, tmp_path) -> None:
        """Verify that empty/None bibliographic fields are omitted entirely from YAML output."""
        source_path = tmp_path / "paper.pdf"
        fm_str = build_frontmatter(
            item_key="ATT000",
            parent_item_type="journalArticle",
            title="Minimal Paper",
            collections=[],
            source_path=source_path,
            source_hash="hash000",
            content_type="application/pdf",
            converter_id="markitdown",
            converter_version="1.0.0",
            converted_at="2026-03-30T12:00:00Z",
            authors=[],
            year=None,
            publication="",
            doi=None,
            url="   ",
        )

        yaml_content = fm_str.strip("- \n\r")
        parsed = yaml.safe_load(yaml_content)

        assert "authors" not in parsed
        assert "year" not in parsed
        assert "publication" not in parsed
        assert "doi" not in parsed
        assert "url" not in parsed

    def test_build_frontmatter_numeric_string_year_converted_to_int(self, tmp_path) -> None:
        """Verify that a 4-digit string year is cleanly stored as an int in YAML."""
        source_path = tmp_path / "paper.pdf"
        fm_str = build_frontmatter(
            item_key="ATT111",
            parent_item_type="journalArticle",
            title="Year Test",
            collections=[],
            source_path=source_path,
            source_hash="hash111",
            content_type="application/pdf",
            converter_id="markitdown",
            converter_version="1.0.0",
            converted_at="2026-03-30T12:00:00Z",
            year="2023",
        )

        yaml_content = fm_str.strip("- \n\r")
        parsed = yaml.safe_load(yaml_content)
        assert parsed["year"] == 2023
        assert isinstance(parsed["year"], int)

    def test_strip_existing_frontmatter(self) -> None:
        raw_md = "---\ntitle: Old Title\n---\n\n# Real Heading\n\nBody text"
        clean = strip_existing_frontmatter(raw_md)
        assert clean == "# Real Heading\n\nBody text"

    def test_strip_existing_frontmatter_no_frontmatter(self) -> None:
        raw_md = "# Real Heading\n\nBody text"
        clean = strip_existing_frontmatter(raw_md)
        assert clean == raw_md


class TestBatchRootIndexOptimization:
    """Verify root index.md generation behavior during batch syncs."""

    def test_update_root_index_false_skips_root_generation(self, cache_dir, tmp_path) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("content", encoding="utf-8")

        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert(
            item_key="BATCH1",
            source_path=source,
            content_type="text/plain",
            update_root_index=False,
        )

        root_index = cache_dir / "index.md"
        # Root index should not exist yet because update_root_index=False
        assert not root_index.exists()

        # Item index should still be created
        assert (cache_dir / "BATCH1" / "index.md").exists()

        # Regenerate once at end
        processor.regenerate_root_index()
        assert root_index.exists()
        assert "BATCH1" in root_index.read_text(encoding="utf-8")


class TestRelativeSourcePath:
    """Verify _compute_relative_source_path behavior."""

    def test_compute_relative_source_path_standard(self, tmp_path) -> None:
        cache_dir = tmp_path / "zocrates-data" / "corpus"
        source_path = tmp_path / "storage" / "KEY123" / "paper.pdf"

        rel = _compute_relative_source_path(source_path, cache_dir)
        assert rel == "../../storage/KEY123/paper.pdf"

    def test_compute_relative_source_path_bundle_dir(self, tmp_path) -> None:
        bundle_dir = tmp_path / "zocrates-data" / "corpus" / "KEY123"
        source_path = tmp_path / "storage" / "KEY123" / "paper.pdf"

        rel = _compute_relative_source_path(source_path, bundle_dir)
        assert rel == "../../../storage/KEY123/paper.pdf"

    def test_compute_relative_source_path_uri_schemes(self, tmp_path) -> None:
        bundle_dir = tmp_path / "corpus" / "NOTE123"
        assert _compute_relative_source_path("zotero://note/NOTE123", bundle_dir) == "zotero://note/NOTE123"
        assert _compute_relative_source_path("zotero:/note/NOTE123", bundle_dir) == "zotero:/note/NOTE123"
        assert _compute_relative_source_path("https://example.com/paper.pdf", bundle_dir) == "https://example.com/paper.pdf"
        assert _compute_relative_source_path("file:///path/to/paper.pdf", bundle_dir) == "file:///path/to/paper.pdf"

    def test_compute_relative_source_path_fallback_on_error(self, tmp_path, monkeypatch) -> None:
        bundle_dir = tmp_path / "corpus" / "KEY123"
        source_path = tmp_path / "storage" / "KEY123" / "paper.pdf"

        def _mock_relpath(path, start):
            raise ValueError("Different drives or inaccessible path")

        import os
        monkeypatch.setattr(os.path, "relpath", _mock_relpath)
        rel = _compute_relative_source_path(source_path, bundle_dir)
        assert rel == Path(source_path).as_posix()

    def test_compute_relative_source_path_empty_or_none(self, tmp_path) -> None:
        cache_dir = tmp_path / "corpus"
        assert _compute_relative_source_path("", cache_dir) == ""
        assert _compute_relative_source_path(None, cache_dir) == ""
        assert _compute_relative_source_path("   ", cache_dir) == ""


class TestCatalogGeneration:
    """Verify item-level and root-level index.md generation and AGENTS.md."""

    def test_generate_item_index_md_images_and_tables(self, tmp_path) -> None:
        item_dir = tmp_path / "ITEM_CAT"
        item_dir.mkdir()
        (item_dir / "document.md").write_text("content", encoding="utf-8")

        images_dir = item_dir / "images"
        images_dir.mkdir()
        (images_dir / "img-000.PNG").write_bytes(b"png")
        (images_dir / "diagram.SVG").write_bytes(b"svg")

        tables_dir = item_dir / "tables"
        tables_dir.mkdir()
        (tables_dir / "table_001.CSV").write_text("a,b,c", encoding="utf-8")
        (tables_dir / "table_001.html").write_text("<table></table>", encoding="utf-8")

        generate_item_index_md(
            item_dir=item_dir,
            item_key="ITEM_CAT",
            title="My Paper Title",
            converter_id="markitdown",
            converter_version="1.0.0",
        )

        index_path = item_dir / "index.md"
        assert index_path.exists()
        content = index_path.read_text(encoding="utf-8")

        assert "# Item Catalog: ITEM_CAT" in content
        assert "**Title**: My Paper Title" in content
        assert "**Converter**: markitdown (v1.0.0)" in content
        assert "[document.md](./document.md)" in content
        assert "### Images" in content
        assert "[images/img-000.PNG](./images/img-000.PNG)" in content
        assert "[images/diagram.SVG](./images/diagram.SVG)" in content
        assert "### Tables" in content
        assert "[tables/table_001.CSV](./tables/table_001.CSV)" in content
        assert "[tables/table_001.html](./tables/table_001.html)" in content

    def test_generate_item_index_md_no_assets(self, tmp_path) -> None:
        item_dir = tmp_path / "ITEM_EMPTY"
        item_dir.mkdir()
        (item_dir / "document.md").write_text("content", encoding="utf-8")

        generate_item_index_md(
            item_dir=item_dir,
            item_key="ITEM_EMPTY",
            title="Empty Assets Paper",
            converter_id="direct_read",
            converter_version="1.0.0",
        )

        index_path = item_dir / "index.md"
        content = index_path.read_text(encoding="utf-8")
        assert "## Assets\nNone" in content

    def test_generate_root_index_md(self, tmp_path) -> None:
        cache_dir = tmp_path / "zocrates-data" / "corpus"
        storage_dir = tmp_path / "storage" / "ITEM_ROOT1"
        storage_dir.mkdir(parents=True)
        source = storage_dir / "paper.pdf"
        source.write_text("pdf bytes", encoding="utf-8")

        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert(
            item_key="ITEM_ROOT1",
            source_path=source,
            content_type="application/pdf",
            parent_item_type="journalArticle",
            title="Root Test | Paper with Pipes\nand Newlines",
        )

        root_index = cache_dir / "index.md"
        assert root_index.exists()
        content = root_index.read_text(encoding="utf-8")

        assert "# Zocrates Knowledge Base Catalog" in content
        assert "Total Converted Documents: 1" in content
        assert (
            "| Item Key | Title | Type | Converter | Converted At | Source Path | Chars |"
            in content
        )
        assert (
            "| [ITEM_ROOT1](./ITEM_ROOT1/document.md) | Root Test \\| Paper with Pipes and Newlines | journalArticle |"
            in content
        )
        assert "../../storage/ITEM_ROOT1/paper.pdf" in content

        # Verify AGENTS.md was also generated automatically
        agents_md = cache_dir / "AGENTS.md"
        assert agents_md.exists()
        agents_text = agents_md.read_text(encoding="utf-8")
        assert "# AGENTS.md - Zocrates Corpus Navigation Guide" in agents_text
        assert "Step 1: Catalog Discovery" in agents_text
        assert "docling_document.json" in agents_text

    def test_generate_root_index_md_legacy_title_fallback_and_backfill(
        self, cache_dir, tmp_path
    ) -> None:
        # Simulate legacy DB record with title = NULL and document.md containing title in YAML
        item_dir = cache_dir / "LEGACY1"
        item_dir.mkdir(parents=True)
        doc_content = "---\ntitle: 'Recovered Legacy Paper Title'\ntype: journalArticle\n---\n# Content"
        (item_dir / "document.md").write_text(doc_content, encoding="utf-8")

        with _open_meta_db(cache_dir) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta
                    (item_key, title, converter_id, converter_version, source_path,
                     content_type, parent_item_type, converted_at, source_hash, char_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "LEGACY1",
                    None,
                    "docling_local",
                    "2.0.0",
                    str(tmp_path / "paper.pdf"),
                    "application/pdf",
                    "journalArticle",
                    "2026-01-01T00:00:00Z",
                    "dummyhash",
                    len(doc_content),
                ),
            )
            conn.commit()

        generate_root_index_md(cache_dir)

        root_index = cache_dir / "index.md"
        content = root_index.read_text(encoding="utf-8")
        assert "Recovered Legacy Paper Title" in content

        # Verify backfill updated the database
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute("SELECT title FROM conversion_meta WHERE item_key = 'LEGACY1'").fetchone()
            assert row["title"] == "Recovered Legacy Paper Title"


class TestProcessorFrontmatterIntegration:
    """Verify DocumentProcessor integrates frontmatter and catalog generation."""

    def test_processor_writes_frontmatter_and_catalogs(
        self, cache_dir, tmp_path
    ) -> None:
        source = tmp_path / "doc.txt"
        source.write_text("Original body content", encoding="utf-8")

        processor = DocumentProcessor(cache_dir)
        collections = [ZoteroCollection(name="AI Notes", key="COLL99")]

        res = processor.get_or_convert(
            item_key="FMKEY1",
            source_path=source,
            content_type="text/plain",
            parent_item_type="note",
            title="FM Test Note",
            collections=collections,
        )

        assert res is not None
        assert res.startswith("---\n")
        assert "type: note" in res
        assert "title: FM Test Note" in res
        assert "name: AI Notes" in res
        assert "collection_key: COLL99" in res
        assert "Original body content" in res

        # Verify source.path in document.md frontmatter is relative to bundle_dir
        parsed = yaml.safe_load(res.split("---")[1])
        assert parsed["source"]["path"] == "../../doc.txt"

        # Check item index.md
        item_index = cache_dir / "FMKEY1" / "index.md"
        assert item_index.exists()

        # Check root index.md and AGENTS.md
        root_index = cache_dir / "index.md"
        assert root_index.exists()
        assert (cache_dir / "AGENTS.md").exists()

        # Verify metadata retrieval contains title
        meta = processor.get_metadata("FMKEY1")
        assert meta is not None
        assert meta["title"] == "FM Test Note"

        all_meta = processor.get_all_metadata()
        assert "FMKEY1" in all_meta
        assert all_meta["FMKEY1"]["title"] == "FM Test Note"
