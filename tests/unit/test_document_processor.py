"""Unit tests for the document processor cache and conversion logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.rag.document_processor import (
    DocumentProcessor,
)
from zocrates.rag.document_converters import BaseDocumentConverter, DirectReadConverter


@pytest.fixture
def temp_cache_dir(tmp_path) -> Path:
    """Provide a temporary directory path for the Markdown cache under tmp_path."""
    return tmp_path / "md_cache"


def test_document_processor_cache_dir_property(temp_cache_dir):
    """Verify that DocumentProcessor.cache_dir property exposes the cache directory Path."""
    processor = DocumentProcessor(cache_dir=temp_cache_dir)
    assert processor.cache_dir == temp_cache_dir


def test_document_processor_conversion_and_cache(temp_cache_dir, tmp_path):
    """Verify cache hit/miss semantics and hash-based invalidation.

    Asserts:
      1. First call converts via MarkItDown and writes ``<item_key>/document.md``.
      2. Second call with the same source returns cached content without
         re-invoking the converter.
      3. Third call after the source file changes recomputes and updates the
         cache (SHA-256 mismatch triggers re-conversion).
    """
    # Create a dummy pdf file on disk
    source_file = tmp_path / "paper.pdf"
    source_file.write_text("dummy pdf contents", encoding="utf-8")

    # Mock MarkItDown
    with patch("zocrates.rag.document_converters.MarkItDown") as mock_mid_cls:
        mock_mid = MagicMock()
        mock_convert_result = MagicMock()
        mock_convert_result.text_content = "## Converted Markdown Content"
        mock_mid.convert.return_value = mock_convert_result
        mock_mid_cls.return_value = mock_mid

        processor = DocumentProcessor(temp_cache_dir)

        # 1. First call: Should perform conversion and save to cache
        markdown = processor.get_or_convert(
            "item_abc123", source_file, "application/pdf"
        )
        assert "## Converted Markdown Content" in markdown
        mock_mid.convert.assert_called_once_with(str(source_file))

        # Check that file was cached in item-centric bundle
        cache_file = temp_cache_dir / "item_abc123" / "document.md"
        assert cache_file.exists()
        assert "## Converted Markdown Content" in cache_file.read_text(encoding="utf-8")

        # 2. Second call: Should read from cache without calling conversion again
        mock_mid.reset_mock()
        markdown2 = processor.get_or_convert(
            "item_abc123", source_file, "application/pdf"
        )
        assert "## Converted Markdown Content" in markdown2
        mock_mid.convert.assert_not_called()

        # 3. Third call: Change source file contents (hashing mismatch) -> should re-convert
        source_file.write_text("updated pdf contents", encoding="utf-8")
        mock_convert_result.text_content = "## Converted Markdown Content v2"

        markdown3 = processor.get_or_convert(
            "item_abc123", source_file, "application/pdf"
        )
        assert "## Converted Markdown Content v2" in markdown3
        mock_mid.convert.assert_called_once_with(str(source_file))


def test_direct_read_converter(temp_cache_dir, tmp_path):
    """Verify DirectReadConverter handles markdown files directly without MarkItDown."""
    md_file = tmp_path / "notes.md"
    md_file.write_text("# Direct Note\n\nSome content.", encoding="utf-8")

    processor = DocumentProcessor(temp_cache_dir)
    result = processor.get_or_convert("item_md1", md_file, "text/markdown")
    assert "# Direct Note" in result


def test_custom_converter_registration(temp_cache_dir, tmp_path):
    """Verify custom BaseDocumentConverter registration and priority dispatch."""

    class MockCustomConverter(BaseDocumentConverter):

        @property
        def converter_id(self) -> str:
            return "mock_custom"

        def supports(self, content_type: str, source_path: Path) -> bool:
            return content_type == "application/custom-doc"

        def convert(
            self,
            item_key: str,
            source_path: Path,
            content_type: str,
            cache_dir: Path,
        ) -> str | None:
            return f"# Custom Converted: {source_path.name}"

    dummy_doc = tmp_path / "spec.custom"
    dummy_doc.write_text("custom payload", encoding="utf-8")

    processor = DocumentProcessor(temp_cache_dir)
    processor.register_converter(MockCustomConverter())

    result = processor.get_or_convert(
        "item_custom1", dummy_doc, "application/custom-doc"
    )
    assert "# Custom Converted: spec.custom" in result


def test_conversion_failures_lifecycle(temp_cache_dir, tmp_path):
    """Verify recording, retrieving, and clearing conversion failure records."""
    processor = DocumentProcessor(temp_cache_dir)

    # 1. Initially no failures
    assert processor.get_failures("ITEM_FAIL1") == {}

    # 2. Record failure
    processor.record_failure(
        item_key="ITEM_FAIL1",
        converter_id="docling_local",
        error_message="CUDA out of memory",
        source_hash="hash123",
    )
    failures = processor.get_failures("ITEM_FAIL1")
    assert "docling_local" in failures
    assert failures["docling_local"]["error_message"] == "CUDA out of memory"
    assert failures["docling_local"]["source_hash"] == "hash123"
    assert "failed_at" in failures["docling_local"]

    # 3. Record second failure with another converter
    processor.record_failure(
        item_key="ITEM_FAIL1",
        converter_id="other_converter",
        error_message="Format unsupported",
        source_hash="hash123",
    )
    failures = processor.get_failures("ITEM_FAIL1")
    assert len(failures) == 2
    assert "other_converter" in failures

    # 4. Clear single converter failure
    processor.clear_failure("ITEM_FAIL1", "docling_local")
    failures = processor.get_failures("ITEM_FAIL1")
    assert "docling_local" not in failures
    assert "other_converter" in failures

    # 5. Clear all failures for item
    processor.clear_failure("ITEM_FAIL1")
    assert processor.get_failures("ITEM_FAIL1") == {}


def test_convert_with_failure_records_and_clears(temp_cache_dir, tmp_path):
    """Verify that convert_with records failure on error and clears on subsequent success."""
    class FailingConverter(BaseDocumentConverter):
        def __init__(self):
            self.should_fail = True

        @property
        def converter_id(self) -> str:
            return "failing_engine"

        def supports(self, content_type: str, source_path: Path) -> bool:
            return True

        def convert(self, item_key: str, source_path: Path, content_type: str, cache_dir: Path) -> str | None:
            if self.should_fail:
                raise RuntimeError("Simulated OOM crash")
            return "# Success Content"

    conv = FailingConverter()
    processor = DocumentProcessor(temp_cache_dir, converters=[conv])

    doc_file = tmp_path / "test.pdf"
    doc_file.write_text("pdf content", encoding="utf-8")

    # 1. Conversion fails
    with pytest.raises(RuntimeError, match="Simulated OOM crash"):
        processor.convert_with("KEY_TEST", "failing_engine", doc_file, "application/pdf")

    failures = processor.get_failures("KEY_TEST")
    assert "failing_engine" in failures
    assert "Simulated OOM crash" in failures["failing_engine"]["error_message"]

    # 2. Conversion succeeds on retry
    conv.should_fail = False
    result = processor.convert_with("KEY_TEST", "failing_engine", doc_file, "application/pdf")
    assert "# Success Content" in result
    assert processor.get_failures("KEY_TEST") == {}


def test_delete_cached_cleans_conversion_failures(temp_cache_dir, tmp_path):
    """Verify that delete_cached removes both metadata and recorded failure rows."""
    processor = DocumentProcessor(temp_cache_dir)
    doc_file = tmp_path / "note.md"
    doc_file.write_text("# Test", encoding="utf-8")

    processor.get_or_convert("DEL_KEY", doc_file, "text/markdown")
    processor.record_failure("DEL_KEY", "docling_local", "CUDA OOM", "hash123")

    assert processor.get_metadata("DEL_KEY") is not None
    assert "docling_local" in processor.get_failures("DEL_KEY")

    processor.delete_cached("DEL_KEY")

    assert processor.get_metadata("DEL_KEY") is None
    assert processor.get_failures("DEL_KEY") == {}


def test_markitdown_html_embedded_image_extraction(temp_cache_dir, tmp_path):
    """Verify that MarkItDownConverter extracts base64 images from HTML snapshots into <figure> blocks."""
    from zocrates.rag.document_converters import MarkItDownConverter

    b64_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    html_file = tmp_path / "snapshot.html"
    html_file.write_text(
        f'<html><body><h1>HTML Article</h1><p>Text before</p><img src="data:image/png;base64,{b64_png}" alt="Benchmark Evaluation Chart" /><p>Text after</p></body></html>',
        encoding="utf-8",
    )

    conv = MarkItDownConverter()
    bundle_dir = temp_cache_dir / "HTML_MID_ITEM"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    result_md = conv.convert(
        item_key="HTML_MID_ITEM",
        source_path=html_file,
        content_type="text/html",
        cache_dir=bundle_dir,
    )

    assert result_md is not None
    # Verify image was extracted to disk
    extracted_images = list((bundle_dir / "images").glob("*.png"))
    assert len(extracted_images) == 1
    assert extracted_images[0].name == "picture_000.png"

    # Verify <figure data-type="image"> block was injected
    assert '<figure data-type="image" data-id="figure-000">' in result_md
    assert "picture_000.png" in result_md
    assert "Benchmark Evaluation Chart" in result_md
    assert "data:image/" not in result_md


def test_document_processor_saves_frontmatter_metadata(temp_cache_dir, tmp_path):
    """Verify that get_or_convert writes bibliographic fields into frontmatter."""
    import yaml

    processor = DocumentProcessor(temp_cache_dir)
    doc_file = tmp_path / "paper.md"
    doc_file.write_text("# Transformer Research\n\nContent here.", encoding="utf-8")

    md = processor.get_or_convert(
        item_key="TRANS_KEY",
        source_path=doc_file,
        content_type="text/markdown",
        parent_item_type="journalArticle",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish", "Shazeer, Noam"],
        year="2017",
        publication="NeurIPS",
        doi="10.48550/arXiv.1706.03762",
        url="https://arxiv.org/abs/1706.03762",
    )

    assert md is not None
    fm_raw = md.split("---")[1]
    parsed = yaml.safe_load(fm_raw)

    assert parsed["title"] == "Attention Is All You Need"
    assert parsed["authors"] == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert parsed["year"] == 2017
    assert parsed["publication"] == "NeurIPS"
    assert parsed["doi"] == "10.48550/arXiv.1706.03762"
    assert parsed["url"] == "https://arxiv.org/abs/1706.03762"


def test_document_processor_convert_with_saves_frontmatter_metadata(temp_cache_dir, tmp_path):
    """Verify that convert_with writes bibliographic fields into frontmatter."""
    import yaml

    processor = DocumentProcessor(temp_cache_dir)
    doc_file = tmp_path / "paper_cw.md"
    doc_file.write_text("# Direct Conversion\n\nBody.", encoding="utf-8")

    md = processor.convert_with(
        item_key="CW_META_KEY",
        converter_id="direct_read",
        source_path=doc_file,
        content_type="text/markdown",
        parent_item_type="preprint",
        title="ColPali Retrieval",
        authors=["Faysse, Manuel", "Sibille, Hugues"],
        year="2025",
        publication="arXiv",
        doi="https://doi.org/10.48550/arXiv.2407.01449",
        url="http://arxiv.org/abs/2407.01449",
    )

    assert md is not None
    fm_raw = md.split("---")[1]
    parsed = yaml.safe_load(fm_raw)

    assert parsed["title"] == "ColPali Retrieval"
    assert parsed["authors"] == ["Faysse, Manuel", "Sibille, Hugues"]
    assert parsed["year"] == 2025
    assert parsed["publication"] == "arXiv"
    assert parsed["doi"] == "https://doi.org/10.48550/arXiv.2407.01449"
    assert parsed["url"] == "http://arxiv.org/abs/2407.01449"


def test_document_converters_vc_redist_runtime_error_on_onnxruntime_dll_failure(tmp_path):
    """Verify document_converters raises friendly RuntimeError when onnxruntime DLL load fails."""
    import builtins
    import zocrates.rag.document_converters as dc
    from zocrates.rag.document_converters import (
        DoclingLocalConverter,
        MarkItDownConverter,
        VC_REDIST_WINDOWS_MSG,
    )

    dummy_doc = tmp_path / "paper.pdf"
    dummy_doc.write_text("test pdf", encoding="utf-8")
    cache_bundle_dir = tmp_path / "TEST_BUNDLE"
    cache_bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Test __getattr__("MarkItDown") with onnxruntime DLL failure
    orig_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "markitdown":
            raise ImportError(
                "DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found."
            )
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=failing_import):
        with pytest.raises(RuntimeError) as exc_info:
            dc.__getattr__("MarkItDown")
        assert "Microsoft Visual C++ 2015–2022 Redistributable (x64) is required on Windows" in str(exc_info.value)
        assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in str(exc_info.value)
        assert str(exc_info.value) == VC_REDIST_WINDOWS_MSG

    # 2. Test MarkItDownConverter.convert() during convert execution
    mock_md = MagicMock()
    mock_md.convert.side_effect = Exception(
        "DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found."
    )
    mid_conv = MarkItDownConverter(markitdown_instance=mock_md)
    with pytest.raises(RuntimeError) as exc_info:
        mid_conv.convert("TEST_ITEM", dummy_doc, "application/pdf", cache_bundle_dir)
    assert "Microsoft Visual C++ 2015–2022 Redistributable (x64) is required on Windows" in str(exc_info.value)
    assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in str(exc_info.value)

    # 3. Test DoclingLocalConverter.convert() during layout analysis / converter call
    docling_conv = DoclingLocalConverter()
    mock_docling = MagicMock()
    mock_docling.convert.side_effect = ImportError(
        "DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found."
    )
    docling_conv._converter = mock_docling
    with pytest.raises(RuntimeError) as exc_info:
        docling_conv.convert("TEST_ITEM", dummy_doc, "application/pdf", cache_bundle_dir)
    assert "Microsoft Visual C++ 2015–2022 Redistributable (x64) is required on Windows" in str(exc_info.value)
    assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in str(exc_info.value)







