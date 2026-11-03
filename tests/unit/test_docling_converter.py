"""Unit tests for DoclingLocalConverter strategy and bundle extraction."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
from PIL import Image
import pytest

from zocrates.rag.document_converters import (
    DoclingLocalConverter,
    _get_package_version,
    _inject_docling_images,
)
from zocrates.rag.document_processor import (
    DocumentProcessor,
    _open_meta_db,
)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "md_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy pdf content")
    return pdf


@pytest.fixture
def sample_html(tmp_path: Path) -> Path:
    html = tmp_path / "sample.html"
    html.write_text("<html><body><h1>Test</h1></body></html>", encoding="utf-8")
    return html


class TestDoclingLocalConverterIdentity:
    """Verify identity contract and support methods for DoclingLocalConverter."""

    def test_identity_properties(self) -> None:
        converter = DoclingLocalConverter()
        assert converter.converter_id == "docling_local"
        version = converter.converter_version
        assert isinstance(version, str)
        assert len(version) > 0

    def test_supports_pdf_and_html(self, sample_pdf: Path, sample_html: Path) -> None:
        converter = DoclingLocalConverter()
        assert converter.supports("application/pdf", sample_pdf) is True
        assert converter.supports("text/html", sample_html) is True
        assert converter.supports("application/xhtml+xml", sample_html) is True

    def test_supports_office_formats(self, tmp_path: Path) -> None:
        converter = DoclingLocalConverter()
        docx = tmp_path / "doc.docx"
        pptx = tmp_path / "slides.pptx"
        assert converter.supports("application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx) is True
        assert converter.supports("application/vnd.openxmlformats-officedocument.presentationml.presentation", pptx) is True

    def test_unsupported_formats(self, tmp_path: Path) -> None:
        converter = DoclingLocalConverter()
        audio = tmp_path / "audio.mp3"
        assert converter.supports("audio/mpeg", audio) is False


class TestDoclingLocalConverterExecution:
    """Verify conversion, asset extraction, and bundle generation using mock Docling objects."""

    def test_mocked_conversion_with_images_and_tables(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        # Mock Docling AST and DocumentConverter
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = (
            "# Extracted Title\n\nSome paragraph text.\n\n<!-- image -->"
        )

        # Mock save_as_json
        def mock_save_as_json(path: Path, *args, **kwargs):
            path.write_text(json.dumps({"ast": "test_ast"}), encoding="utf-8")

        mock_doc.save_as_json.side_effect = mock_save_as_json

        # Mock Picture with caption and provenance
        mock_pic = MagicMock()
        mock_pic.caption_text.return_value = "Figure 1: Test Chart"
        mock_pic.prov = [MagicMock(page_no=1)]
        pil_img = Image.new("RGB", (50, 50), color="blue")
        mock_pic.get_image.return_value = pil_img
        mock_doc.pictures = [mock_pic]

        # Mock Table
        mock_table = MagicMock()
        df = pd.DataFrame({"Col A": [1, 2], "Col B": ["X", "Y"]})
        mock_table.export_to_dataframe.return_value = df
        mock_table.export_to_html.return_value = "<table><tr><td>1</td></tr></table>"
        mock_doc.tables = [mock_table]

        # Mock conversion result
        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc

        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.return_value = mock_conv_res

        converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        item_bundle_dir = cache_dir / "ITEM123"
        item_bundle_dir.mkdir(parents=True, exist_ok=True)

        md = converter.convert(
            item_key="ITEM123",
            source_path=sample_pdf,
            content_type="application/pdf",
            cache_dir=item_bundle_dir,
        )

        assert md is not None
        assert "# Extracted Title" in md
        assert "<!-- image -->" not in md
        assert '<figure data-type="image" data-id="figure-000">' in md
        assert "![Figure 1 (Page 1)](images/picture_000.png)" in md
        assert "picture_000.png" in md
        assert "Figure 1: Test Chart" in md

        # Verify JSON AST saved
        json_path = item_bundle_dir / "docling_document.json"
        assert json_path.exists()
        assert "test_ast" in json_path.read_text(encoding="utf-8")

        # Verify Image extracted
        img_path = item_bundle_dir / "images" / "picture_000.png"
        assert img_path.exists()
        loaded_img = Image.open(img_path)
        assert loaded_img.size == (50, 50)

        # Verify Table extracted (CSV and HTML)
        csv_path = item_bundle_dir / "tables" / "table_000.csv"
        html_path = item_bundle_dir / "tables" / "table_000.html"
        assert csv_path.exists()
        assert "Col A,Col B" in csv_path.read_text(encoding="utf-8")
        assert html_path.exists()
        assert "<table>" in html_path.read_text(encoding="utf-8")

    def test_inject_docling_images_helper(self, tmp_path: Path) -> None:
        mock_doc = MagicMock()
        pic0 = MagicMock()
        pic0.caption_text.return_value = "Diagram A"
        pic0.prov = [MagicMock(page_no=1)]

        pic1 = MagicMock()
        pic1.caption_text.return_value = ""
        pic1.prov = [MagicMock(page_no=2)]

        mock_doc.pictures = [pic0, pic1]

        img0 = tmp_path / "img0.png"
        img1 = tmp_path / "img1.png"
        img0.touch()
        img1.touch()

        # Case 1: Markdown with placeholders
        md_text = "Heading\n\n<!-- image -->\n\nMiddle text\n\n<!-- image -->"
        res = _inject_docling_images(md_text, mock_doc, [(0, img0), (1, img1)])
        assert '<figure data-type="image" data-id="figure-000">' in res
        assert "![Figure 1 (Page 1)](images/img0.png)" in res
        assert "Diagram A" in res
        assert '<figure data-type="image" data-id="figure-001">' in res
        assert "![Figure 2 (Page 2)](images/img1.png)" in res
        assert "<!-- image -->" not in res

        # Case 2: Markdown without placeholders (appended)
        no_ph_text = "Plain text without markers."
        res2 = _inject_docling_images(no_ph_text, mock_doc, [(0, img0)])
        assert "Plain text without markers." in res2
        assert '<figure data-type="image" data-id="figure-000">' in res2
        assert "![Figure 1 (Page 1)](images/img0.png)" in res2

        # Case 3: More images than placeholders (leftovers appended without duplication)
        one_ph_text = "Text\n\n<!-- image -->\n\nEnd"
        res3 = _inject_docling_images(one_ph_text, mock_doc, [(0, img0), (1, img1)])
        assert "![Figure 1 (Page 1)](images/img0.png)" in res3
        assert "![Figure 2 (Page 2)](images/img1.png)" in res3
        assert res3.count("![Figure 1 (Page 1)](images/img0.png)") == 1
        assert res3.count("![Figure 2 (Page 2)](images/img1.png)") == 1

    def test_inject_docling_images_body_pictures_alignment(self, tmp_path: Path) -> None:
        """Verify that when header images are skipped in body markdown, placeholders map to the correct pictures."""
        mock_doc = MagicMock()

        # Docling document with 4 pictures total:
        # pic0 (header logo), pic1 (header icon), pic2 (main chart), pic3 (diagram)
        pic0 = MagicMock(self_ref="#/pictures/0")
        pic0.caption_text.return_value = "Header Logo"

        pic1 = MagicMock(self_ref="#/pictures/1")
        pic1.caption_text.return_value = "Header Icon"

        pic2 = MagicMock(self_ref="#/pictures/2")
        pic2.caption_text.return_value = "Main Performance Chart"

        pic3 = MagicMock(self_ref="#/pictures/3")
        pic3.caption_text.return_value = "System Diagram"

        mock_doc.pictures = [pic0, pic1, pic2, pic3]

        # doc.iterate_items() only contains body items (pic2 and pic3), skipping header items pic0 and pic1
        mock_doc.iterate_items.return_value = [
            (MagicMock(), 0),  # heading
            (pic2, 1),
            (MagicMock(), 0),  # text
            (pic3, 1),
        ]

        img0 = tmp_path / "picture_000.png"
        img1 = tmp_path / "picture_001.png"
        img2 = tmp_path / "picture_002.png"
        img3 = tmp_path / "picture_003.png"
        for p in (img0, img1, img2, img3):
            p.touch()

        saved_images = [
            (0, img0),
            (1, img1),
            (2, img2),
            (3, img3),
        ]

        md_text = "Intro\n\n<!-- image -->\n\nResults\n\n<!-- image -->\n\nConclusion"
        result = _inject_docling_images(md_text, mock_doc, saved_images)

        # Placeholder 0 must map to pic2 (img2) and NOT img0
        assert "![Figure 3](images/picture_002.png)" in result
        assert "Main Performance Chart" in result
        assert '<figure data-type="image" data-id="figure-002">' in result

        # Placeholder 1 must map to pic3 (img3) and NOT img1
        assert "![Figure 4](images/picture_003.png)" in result
        assert "System Diagram" in result
        assert '<figure data-type="image" data-id="figure-003">' in result

        # Header images (img0 and img1) should NOT be injected into the body
        assert "images/picture_000.png" not in result
        assert "images/picture_001.png" not in result

    def test_conversion_handles_empty_or_failing_doc(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.side_effect = RuntimeError("Docling crash")

        converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        item_bundle_dir = cache_dir / "ITEM_ERR"
        item_bundle_dir.mkdir(parents=True, exist_ok=True)

        md = converter.convert(
            item_key="ITEM_ERR",
            source_path=sample_pdf,
            content_type="application/pdf",
            cache_dir=item_bundle_dir,
        )
        assert md is None


class TestDocumentProcessorDoclingIntegration:
    """Verify DocumentProcessor dispatch and DB registration with DoclingLocalConverter."""

    def test_convert_with_docling_local(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = "# Scientific Paper\n\nDocling output."
        mock_doc.save_as_json.side_effect = lambda p, *a, **kw: p.write_text("{}", encoding="utf-8")
        mock_doc.pictures = []
        mock_doc.tables = []

        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc
        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.return_value = mock_conv_res

        docling_converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        processor = DocumentProcessor(
            cache_dir=cache_dir,
            converters=[docling_converter],
        )

        full_md = processor.convert_with(
            item_key="PDF_ITEM_1",
            converter_id="docling_local",
            source_path=sample_pdf,
            content_type="application/pdf",
            title="Scientific Paper",
        )

        assert "Scientific Paper" in full_md
        assert "converter_id: docling_local" in full_md

        # Check DB metadata
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT * FROM conversion_meta WHERE item_key = 'PDF_ITEM_1'"
            ).fetchone()
            assert row is not None
            assert row["converter_id"] == "docling_local"
            assert row["char_count"] == len(full_md)

    def test_convert_with_docling_alias(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = "# Docling Alias Paper"
        mock_doc.save_as_json.side_effect = lambda p, *a, **kw: p.write_text("{}", encoding="utf-8")
        mock_doc.pictures = []
        mock_doc.tables = []

        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc
        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.return_value = mock_conv_res

        docling_converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        processor = DocumentProcessor(
            cache_dir=cache_dir,
            converters=[docling_converter],
        )

        full_md = processor.convert_with(
            item_key="PDF_ITEM_ALIAS",
            converter_id="docling",  # alias
            source_path=sample_pdf,
            content_type="application/pdf",
        )
        assert "Docling Alias Paper" in full_md

    def test_html_embedded_image_extraction(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        # Create an HTML file with embedded base64 image
        b64_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        html_file = tmp_path / "snapshot.html"
        html_file.write_text(
            f'<html><body><h1>HTML Paper</h1><img src="data:image/png;base64,{b64_png}" alt="Neural Architecture Diagram" /></body></html>',
            encoding="utf-8",
        )

        mock_doc = MagicMock()
        # Docling HTML export leaves <!-- image --> placeholder
        mock_doc.export_to_markdown.return_value = "# HTML Paper\n\n<!-- image -->\n\nConclusion text."
        mock_doc.save_as_json.side_effect = lambda p, *a, **kw: p.write_text("{}", encoding="utf-8")
        # Docling HTML pic.get_image(doc) returns None
        mock_pic = MagicMock()
        mock_pic.get_image.return_value = None
        mock_pic.caption_text.return_value = "Neural Architecture Diagram"
        mock_doc.pictures = [mock_pic]
        mock_doc.tables = []

        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc
        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.return_value = mock_conv_res

        converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        bundle_dir = cache_dir / "HTML_ITEM"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        result_md = converter.convert(
            item_key="HTML_ITEM",
            source_path=html_file,
            content_type="text/html",
            cache_dir=bundle_dir,
        )

        # Verify extracted image file exists on disk
        extracted_images = list((bundle_dir / "images").glob("*.png"))
        assert len(extracted_images) == 1
        assert extracted_images[0].name == "picture_000.png"

        # Verify figure block was injected and placeholder replaced
        assert "<!-- image -->" not in result_md
        assert '<figure data-type="image" data-id="figure-000">' in result_md
        assert "picture_000.png" in result_md
        assert "Neural Architecture Diagram" in result_md


class TestDoclingHardwareAndFallback:
    """Verify hardware-awareness, CPU preference, and CUDA OOM fallback handling."""

    def test_cuda_oom_triggers_cpu_fallback(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        # Mock first converter raising CUDA OutOfMemoryError
        mock_gpu_conv = MagicMock()
        mock_gpu_conv.convert.side_effect = RuntimeError("CUDA out of memory. Tried to allocate 2.00 MiB.")

        # Mock fallback CPU converter succeeding
        mock_cpu_doc = MagicMock()
        mock_cpu_doc.export_to_markdown.return_value = "# Recovered via CPU"
        mock_cpu_doc.save_as_json.side_effect = lambda p, *a, **kw: p.write_text("{}", encoding="utf-8")
        mock_cpu_doc.pictures = []
        mock_cpu_doc.tables = []

        mock_cpu_res = MagicMock()
        mock_cpu_res.document = mock_cpu_doc
        mock_cpu_conv = MagicMock()
        mock_cpu_conv.convert.return_value = mock_cpu_res

        converter = DoclingLocalConverter(converter_instance=mock_gpu_conv)
        # Mock _create_converter_instance to return the CPU converter on fallback
        converter._create_converter_instance = MagicMock(return_value=mock_cpu_conv)

        item_bundle_dir = cache_dir / "ITEM_OOM"
        item_bundle_dir.mkdir(parents=True, exist_ok=True)

        md = converter.convert(
            item_key="ITEM_OOM",
            source_path=sample_pdf,
            content_type="application/pdf",
            cache_dir=item_bundle_dir,
        )

        assert md == "# Recovered via CPU"
        converter._create_converter_instance.assert_called_once_with(force_cpu=True)

    def test_convert_with_replaces_tmp_dir_uris_in_markdown(
        self, cache_dir: Path, sample_pdf: Path
    ) -> None:
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = "Heading\n\n<!-- image -->\n\nText"
        mock_doc.save_as_json.side_effect = lambda p, *a, **kw: p.write_text("{}", encoding="utf-8")

        mock_pic = MagicMock()
        mock_pic.get_image.return_value = Image.new("RGB", (20, 20), color="green")
        mock_pic.caption_text.return_value = "Chart"
        mock_doc.pictures = [mock_pic]
        mock_doc.tables = []

        mock_conv_res = MagicMock()
        mock_conv_res.document = mock_doc
        mock_converter_inst = MagicMock()
        mock_converter_inst.convert.return_value = mock_conv_res

        docling_converter = DoclingLocalConverter(converter_instance=mock_converter_inst)
        processor = DocumentProcessor(
            cache_dir=cache_dir,
            converters=[docling_converter],
        )

        full_md = processor.convert_with(
            item_key="CLEAN_URI_ITEM",
            converter_id="docling_local",
            source_path=sample_pdf,
            content_type="application/pdf",
        )

        assert ".tmp_" not in full_md
        assert "![Figure 1](images/picture_000.png)" in full_md
        assert "Chart" in full_md

    def test_docling_uses_fast_image_processor(self) -> None:
        from transformers import AutoImageProcessor

        converter = DoclingLocalConverter()
        try:
            converter._create_converter_instance(force_cpu=True)
            # Verify AutoImageProcessor.from_pretrained uses use_fast=True by default
            mock_orig = MagicMock(return_value="fast_processor")
            AutoImageProcessor._orig_from_pretrained = mock_orig
            res = AutoImageProcessor.from_pretrained("dummy_model_path")
            assert res == "fast_processor"
            mock_orig.assert_called_once_with("dummy_model_path", use_fast=True)
        except Exception:
            pass

    def test_docling_modes_and_version_suffix(self, monkeypatch) -> None:
        converter = DoclingLocalConverter()

        # 1. Fast mode -> -fast suffix
        monkeypatch.setenv("DOCUMENT_CONVERTER_DOCLING_MODE", "fast")
        assert converter.converter_version.endswith("-fast")

        # 2. Full mode -> no fast suffix
        monkeypatch.setenv("DOCUMENT_CONVERTER_DOCLING_MODE", "full")
        assert not converter.converter_version.endswith("-fast")

        # 3. Auto mode with low-VRAM GPU -> -fast suffix
        monkeypatch.setenv("DOCUMENT_CONVERTER_DOCLING_MODE", "auto")
        monkeypatch.setattr("zocrates.rag.hardware.is_high_performance_gpu", lambda: False)
        assert converter.converter_version.endswith("-fast")

        # 4. Auto mode with high-perf GPU -> no fast suffix
        monkeypatch.setattr("zocrates.rag.hardware.is_high_performance_gpu", lambda: True)
        monkeypatch.setattr("zocrates.config.get_force_cpu_pref", lambda *a, **k: False)
        assert not converter.converter_version.endswith("-fast")

