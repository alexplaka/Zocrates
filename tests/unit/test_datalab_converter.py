"""Unit tests for DatalabConverter and Datalab integration."""

import base64
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zocrates.rag.document_converters import (
    DatalabConverter,
    _format_datalab_markdown,
    generate_markdown_from_datalab_ast,
)
from zocrates.rag.document_processor import DocumentProcessor
from zocrates.rag.bundle_catalog import strip_existing_frontmatter


@dataclass
class DummyDatalabResult:
    """Mock result object mimicking datalab_sdk.ConversionResult."""

    success: bool = True
    output_format: str = "markdown,json"
    markdown: str | None = None
    json: dict[str, Any] | None = None
    images: dict[str, Any] | None = None
    parse_quality_score: float | None = 4.5
    checkpoint_id: str | None = "test-checkpoint-uuid-123"
    runtime: float | None = 2.1
    page_count: int | None = 3
    error: str | None = None


class TestDatalabConverterIdentity:
    """Test converter metadata and supported formats."""

    def test_identity_properties(self):
        conv = DatalabConverter()
        assert conv.converter_id == "datalab"
        assert conv.is_remote is True
        assert isinstance(conv.converter_version, str)

    def test_supported_formats_pdf_only(self, tmp_path):
        conv = DatalabConverter()

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 header")
        assert conv.supports("application/pdf", pdf_path)
        assert conv.supports("application/octet-stream", pdf_path)

        # Extensionless PDF with magic header
        extless_pdf = tmp_path / "VDKHGW8P"
        extless_pdf.write_bytes(b"%PDF-1.5 stream")
        assert conv.supports("application/octet-stream", extless_pdf)

        # Unsupported non-PDF formats (must return False)
        assert not conv.supports("text/html", tmp_path / "page.html")
        assert not conv.supports(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            tmp_path / "doc.docx",
        )
        assert not conv.supports("image/png", tmp_path / "figure.png")
        assert not conv.supports("image/jpeg", tmp_path / "figure.jpg")
        assert not conv.supports("application/epub+zip", tmp_path / "book.epub")
        assert not conv.supports("audio/mpeg", tmp_path / "song.mp3")
        assert not conv.supports("application/x-executable", tmp_path / "binary.bin")


class TestDatalabMarkdownFormatting:
    """Test _format_datalab_markdown semantic block wrapping fallback."""

    def test_format_fenced_synthetic_captions(self):
        raw_md = (
            "**Figure 1** Author description of the experiment.\n\n"
            "![Figure 1: Summary](dbac7ad25f8fe57949caae9b729a9eed_img.jpg)\n"
            "<synthetic_caption>\n"
            "Figure 1 consists of four bar graphs (a, b, c, d) showing step-through latencies.\n"
            "</synthetic_caption>\n\n"
            "Subsequent body paragraph text."
        )
        formatted = _format_datalab_markdown(raw_md)
        assert '<figure data-type="image">' in formatted
        assert '![Figure 1](images/dbac7ad25f8fe57949caae9b729a9eed_img.jpg)' in formatted
        assert "<synthetic_caption>" in formatted
        assert "**Figure 1** Author description of the experiment." in formatted
        assert "</figure>" in formatted

    def test_format_standalone_images_and_tables(self):
        raw_md = (
            "![Simple diagram](diagram.png)\n\n"
            "| Col A | Col B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        formatted = _format_datalab_markdown(raw_md)
        assert '<figure data-type="image">' in formatted
        assert '![Simple diagram](images/diagram.png)' in formatted
        assert '<figure data-type="table">' in formatted
        assert "| Col A | Col B |" in formatted


class TestDatalabASTMarkdownGeneration:
    """Test generate_markdown_from_datalab_ast with DOM trees."""

    def test_figure_with_preceding_caption(self):
        """Test pairing where Caption block comes before Figure/Diagram block (e.g. test_paper Fig 1)."""
        ast = {
            "children": [
                {
                    "block_type": "Page",
                    "id": "/page/0",
                    "children": [
                        {
                            "block_type": "Caption",
                            "id": "/page/0/Caption/1",
                            "html": "<p><b>Figure 1</b> In the passive avoidance task, mutant mice have retarded learning.</p>",
                        },
                        {
                            "block_type": "Figure",
                            "id": "/page/0/Figure/2",
                            "html": (
                                '<img alt="Figure 1: Passive avoidance task results" src="fig_001.jpg"/>'
                                '<div class="img-description"><p>Bar graphs showing latency across genotypes.</p></div>'
                            ),
                        },
                        {
                            "block_type": "Text",
                            "id": "/page/0/Text/3",
                            "html": "<p>Subsequent text paragraph discussing the findings.</p>",
                        },
                    ],
                }
            ]
        }

        md = generate_markdown_from_datalab_ast(ast)
        assert '<figure data-type="image">' in md
        assert "![Figure 1](images/fig_001.jpg)" in md
        assert '<synthetic_caption data-source="datalab">\nBar graphs showing latency across genotypes.\n</synthetic_caption>' in md
        assert "**Figure 1** In the passive avoidance task, mutant mice have retarded learning." in md
        assert "</figure>" in md
        # Caption should not be duplicated outside figure
        assert md.count("Figure 1") == 2  # Once in ![Figure 1] and once in **Figure 1**

    def test_figure_with_succeeding_caption(self):
        """Test pairing where Caption block comes after Diagram block (e.g. Prot2Chat Fig 1, 2)."""
        ast = {
            "children": [
                {
                    "block_type": "Page",
                    "id": "/page/1",
                    "children": [
                        {
                            "block_type": "Diagram",
                            "id": "/page/1/Diagram/1",
                            "html": (
                                '<img alt="Prot2Chat framework" src="diagram_prot2chat.png"/>'
                                '<div class="img-description"><div class="img-alt">Diagram illustrating the model framework.</div></div>'
                            ),
                        },
                        {
                            "block_type": "Caption",
                            "id": "/page/1/Caption/2",
                            "html": "<p><b>Fig. 1.</b> Prot2Chat architecture diagram with early fusion.</p>",
                        },
                    ],
                }
            ]
        }

        md = generate_markdown_from_datalab_ast(ast)
        assert '<figure data-type="image">' in md
        assert "![Fig. 1](images/diagram_prot2chat.png)" in md
        assert '<synthetic_caption data-source="datalab">\nDiagram illustrating the model framework.\n</synthetic_caption>' in md
        assert "**Fig. 1.** Prot2Chat architecture diagram with early fusion." in md
        assert "</figure>" in md

    def test_table_with_preceding_caption(self):
        """Test pairing where Table caption precedes Table block."""
        ast = {
            "children": [
                {
                    "block_type": "Page",
                    "id": "/page/2",
                    "children": [
                        {
                            "block_type": "Caption",
                            "id": "/page/2/Caption/1",
                            "html": "<p><b>Table 1.</b> Comparison of protein Q&amp;A models.</p>",
                        },
                        {
                            "block_type": "Table",
                            "id": "/page/2/Table/2",
                            "html": (
                                "<table>"
                                "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
                                "<tr><td>Prot2Text</td><td>Sequence</td><td>Text</td></tr>"
                                "</table>"
                            ),
                        },
                    ],
                }
            ]
        }

        md = generate_markdown_from_datalab_ast(ast)
        assert '<figure data-type="table">' in md
        assert "**Table 1.** Comparison of protein Q&A models." in md
        assert "| Model | Input | Output |" in md
        assert "| Prot2Text | Sequence | Text |" in md
        assert "</figure>" in md
        # Caption should not be emitted outside the table figure
        assert md.count("Table 1.") == 1

    def test_list_formatting_no_duplicated_markers(self):
        """Test that numbered and bulleted lists strip redundant numbers and bullet glyphs."""
        ast = {
            "children": [
                {
                    "block_type": "Page",
                    "id": "/page/0",
                    "children": [
                        {
                            "block_type": "ListGroup",
                            "id": "/page/0/ListGroup/1",
                            "html": (
                                '<ol style="list-style-type: none">\n'
                                "<li>1. First contribution point.</li>\n"
                                "<li>2. Second contribution point.</li>\n"
                                "</ol>"
                            ),
                        },
                        {
                            "block_type": "ListGroup",
                            "id": "/page/0/ListGroup/2",
                            "html": (
                                '<ul style="list-style-type: none">\n'
                                "<li>• <b>LLaMA3:</b> Baseline description.</li>\n"
                                "<li>• <b>KIMI:</b> Another baseline.</li>\n"
                                "</ul>"
                            ),
                        },
                        {
                            "block_type": "ListGroup",
                            "id": "/page/0/ListGroup/3",
                            "html": (
                                '<ol style="list-style-type: none">\n'
                                "<li>7. Reference starting on page 2.</li>\n"
                                "<li>8. Next reference.</li>\n"
                                "</ol>"
                            ),
                        },
                    ],
                }
            ]
        }

        md = generate_markdown_from_datalab_ast(ast)
        # Check numbered lists don't have "1. 1."
        assert "1. First contribution point." in md
        assert "1. 1." not in md
        assert "2. Second contribution point." in md
        assert "2. 2." not in md

        # Check references starting at 7 preserve index 7
        assert "7. Reference starting on page 2." in md
        assert "8. Next reference." in md
        assert "7. 7." not in md

        # Check bulleted list strips • without breaking bold
        assert "- **LLaMA3:** Baseline description." in md
        assert "- •" not in md
        assert "- **KIMI:** Another baseline." in md

    def test_latex_math_formulas_preservation(self):
        """Test inline ($...$) and block ($$...$$) LaTeX formulas."""
        ast = {
            "children": [
                {
                    "block_type": "Page",
                    "id": "/page/0",
                    "children": [
                        {
                            "block_type": "Text",
                            "id": "/page/0/Text/1",
                            "html": "<p>We study <math>\\alpha</math>CaMKII autophosphorylation at <math>Thr286</math>.</p>",
                        },
                        {
                            "block_type": "Equation",
                            "id": "/page/0/Equation/2",
                            "html": '<p><math display="block">h_E = \\text{Linear}(E) \\quad (1)</math></p>',
                        },
                    ],
                }
            ]
        }

        md = generate_markdown_from_datalab_ast(ast)
        assert "$\\alpha$" in md
        assert "$Thr286$" in md
        assert "$$h_E = \\text{Linear}(E) \\quad (1)$$" in md


class TestDatalabConverterExecution:
    """Test convert() execution paths, adaptive retry, extensionless staging, and asset unpacking."""

    def test_missing_api_key_raises_runtime_error(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")

        with patch("zocrates.config.get_datalab_api_key", return_value=None):
            with pytest.raises(RuntimeError, match="Datalab API key not configured"):
                conv.convert("ITEM1", pdf_path, "application/pdf", tmp_path / "bundle")

    def test_successful_conversion_balanced_mode(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        img_bytes = b"fake_png_data_bytes"
        b64_img = base64.b64encode(img_bytes).decode("ascii")

        mock_res = DummyDatalabResult(
            success=True,
            markdown="Sample text.\n\n![Figure 1](fig1.png)\n<synthetic_caption>Desc</synthetic_caption>",
            json={"pages": [{"page_number": 1, "blocks": []}]},
            images={"fig1.png": b64_img},
            parse_quality_score=4.8,
            checkpoint_id="chk-abc-999",
        )

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_res

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                out_md = conv.convert("ITEM1", pdf_path, "application/pdf", bundle_dir)

        assert out_md is not None
        assert '<figure data-type="image">' in out_md
        assert 'images/fig1.png' in out_md

        # Verify images unpacked
        saved_img = bundle_dir / "images" / "fig1.png"
        assert saved_img.exists()
        assert saved_img.read_bytes() == img_bytes

        # Verify datalab_document.json saved
        ast_file = bundle_dir / "datalab_document.json"
        assert ast_file.exists()
        ast_data = json.loads(ast_file.read_text(encoding="utf-8"))
        assert ast_data.get("parse_quality_score") == 4.8
        assert ast_data.get("checkpoint_id") == "chk-abc-999"

        # Verify metadata dictionary
        meta = conv.get_metadata("ITEM1")
        assert meta["parse_quality_score"] == 4.8
        assert meta["checkpoint_id"] == "chk-abc-999"

    def test_extensionless_pdf_staged_safely(self, tmp_path):
        """Test safeguard for legitimate PDFs lacking a .pdf extension."""
        conv = DatalabConverter()
        extless_path = tmp_path / "ATTACHMENT_RAW"
        extless_path.write_bytes(b"%PDF-1.4 raw binary")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        mock_res = DummyDatalabResult(
            success=True,
            markdown="# Extless PDF Converted\nContent here.",
            parse_quality_score=4.5,
        )

        captured_paths = []

        def mock_convert(src_path, options=None):
            captured_paths.append(Path(src_path))
            return mock_res

        mock_client = MagicMock()
        mock_client.convert.side_effect = mock_convert

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                out_md = conv.convert("ITEM_EXTLESS", extless_path, "application/pdf", bundle_dir)

        assert out_md is not None
        assert len(captured_paths) == 1
        # Target passed to DatalabClient must end with .pdf
        assert captured_paths[0].suffix.lower() == ".pdf"
        assert captured_paths[0].stem == "ATTACHMENT_RAW"

    def test_adaptive_retry_on_low_quality_score(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "noisy_scan.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        # First run in balanced mode produces low score 2.8 (<3.0)
        low_res = DummyDatalabResult(
            success=True,
            markdown="Poor scan text.",
            parse_quality_score=2.8,
            checkpoint_id="chk-low-1",
        )
        # Second run in accurate mode produces high score 4.2
        high_res = DummyDatalabResult(
            success=True,
            markdown="Accurate reconstructed text with LaTeX $E = mc^2$.",
            parse_quality_score=4.2,
            checkpoint_id="chk-high-2",
        )

        mock_client = MagicMock()
        mock_client.convert.side_effect = [low_res, high_res]

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"), \
             patch("zocrates.config.get_datalab_merge_cross_page", return_value=False), \
             patch("datalab_sdk.DatalabClient", return_value=mock_client):
            out_md = conv.convert("ITEM_RETRY", pdf_path, "application/pdf", bundle_dir)

        assert mock_client.convert.call_count == 2
        first_call_opts = mock_client.convert.call_args_list[0][1]["options"]
        second_call_opts = mock_client.convert.call_args_list[1][1]["options"]
        assert first_call_opts.mode == "balanced"
        assert second_call_opts.mode == "accurate"
        assert first_call_opts.merge_cross_page is False
        assert second_call_opts.merge_cross_page is False

        assert "LaTeX $E = mc^2$" in out_md
        meta = conv.get_metadata("ITEM_RETRY")
        assert meta["parse_quality_score"] == 4.2
        assert meta["checkpoint_id"] == "chk-high-2"

    def test_convert_options_instantiated_with_merge_cross_page(self, tmp_path):
        """Verify ConvertOptions is instantiated with merge_cross_page from config in balanced and accurate retry."""
        conv = DatalabConverter()
        pdf_path = tmp_path / "cross_page.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        low_res = DummyDatalabResult(
            success=True,
            markdown="Poor scan.",
            parse_quality_score=2.0,
            checkpoint_id="chk-low",
        )
        high_res = DummyDatalabResult(
            success=True,
            markdown="Accurate merged content.",
            parse_quality_score=4.8,
            checkpoint_id="chk-high",
        )
        mock_client = MagicMock()
        mock_client.convert.side_effect = [low_res, high_res]

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"), \
             patch("zocrates.config.get_datalab_merge_cross_page", return_value=True), \
             patch("datalab_sdk.DatalabClient", return_value=mock_client):
            conv.convert("ITEM_CROSS_PAGE", pdf_path, "application/pdf", bundle_dir)

        assert mock_client.convert.call_count == 2
        opts_balanced = mock_client.convert.call_args_list[0][1]["options"]
        opts_accurate = mock_client.convert.call_args_list[1][1]["options"]
        assert opts_balanced.merge_cross_page is True
        assert opts_accurate.merge_cross_page is True

    def test_raw_binary_image_unpacking(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        raw_binary_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        mock_res = DummyDatalabResult(
            success=True,
            markdown="Text with binary image: ![Bin](raw_img.png)",
            images={"raw_img.png": raw_binary_img},
            parse_quality_score=4.9,
            checkpoint_id="chk-bin-1",
        )

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_res

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                conv.convert("ITEM_BIN", pdf_path, "application/pdf", bundle_dir)

        saved_img = bundle_dir / "images" / "raw_img.png"
        assert saved_img.exists()
        assert saved_img.read_bytes() == raw_binary_img


class TestDocumentProcessorDatalabIntegration:
    """Test full DocumentProcessor pipeline with DatalabConverter."""

    def test_convert_with_datalab_persists_score_and_checkpoint(self, tmp_path):
        cache_dir = tmp_path / "cache"
        dp = DocumentProcessor(cache_dir=cache_dir)

        pdf_path = tmp_path / "math_paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 math contents")

        mock_res = DummyDatalabResult(
            success=True,
            markdown="# Complex Paper\n\nEquations: $$\\nabla \\times \\mathbf{B} = \\mu_0 \\mathbf{J}$$\n\n![Fig 1](fig.png)",
            json={"pages": []},
            images={"fig.png": base64.b64encode(b"img_bytes").decode("ascii")},
            parse_quality_score=4.85,
            checkpoint_id="chk-math-12345",
        )

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_res

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                doc_md = dp.convert_with(
                    item_key="MATH1",
                    converter_id="datalab",
                    source_path=pdf_path,
                    content_type="application/pdf",
                    title="Maxwell Equations in Matter",
                )

        assert doc_md is not None
        assert "converter_id: datalab" in doc_md
        assert "parse_quality_score: 4.85" in doc_md
        assert "checkpoint_id: chk-math-12345" in doc_md

        # Verify SQLite DB
        meta = dp.get_metadata("MATH1")
        assert meta is not None
        assert meta["converter_id"] == "datalab"
        assert meta["parse_quality_score"] == 4.85
        assert meta["checkpoint_id"] == "chk-math-12345"

    def test_parse_quality_score_nested_json_fallback(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        mock_res = DummyDatalabResult(
            success=True,
            markdown="Text content",
            json={"parse_quality_score": 4.65},
            parse_quality_score=None,  # top-level None
            checkpoint_id="chk-nested-1",
        )

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_res

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                conv.convert("ITEM_NESTED", pdf_path, "application/pdf", bundle_dir)

        meta = conv.get_metadata("ITEM_NESTED")
        assert meta["parse_quality_score"] == 4.65

    def test_parse_quality_score_nested_metadata_fallback(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        @dataclass
        class ResultWithMetadataDict:
            success: bool = True
            output_format: str = "markdown,json"
            markdown: str = "Text content"
            json: dict | None = None
            metadata: dict | None = None
            parse_quality_score: float | None = None
            checkpoint_id: str = "chk-meta-dict"

        mock_res = ResultWithMetadataDict(
            metadata={"parse_quality_score": 4.35},
        )

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_res

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                conv.convert("ITEM_META_DICT", pdf_path, "application/pdf", bundle_dir)

        meta = conv.get_metadata("ITEM_META_DICT")
        assert meta["parse_quality_score"] == 4.35

    def test_parse_quality_score_zero_is_valid(self, tmp_path):
        conv = DatalabConverter()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        # Score of 0.0 should be preserved and trigger retry because 0.0 < 3.0
        mock_low = DummyDatalabResult(
            success=True,
            markdown="Bad scan text",
            json={"parse_quality_score": 0.0},
            parse_quality_score=None,
            checkpoint_id="chk-zero",
        )
        mock_high = DummyDatalabResult(
            success=True,
            markdown="Accurate text",
            parse_quality_score=3.8,
            checkpoint_id="chk-retry",
        )

        mock_client = MagicMock()
        mock_client.convert.side_effect = [mock_low, mock_high]

        with patch("zocrates.config.get_datalab_api_key", return_value="fake-datalab-key"):
            with patch("datalab_sdk.DatalabClient", return_value=mock_client):
                conv.convert("ITEM_ZERO", pdf_path, "application/pdf", bundle_dir)

        assert mock_client.convert.call_count == 2
        meta = conv.get_metadata("ITEM_ZERO")
        assert meta["parse_quality_score"] == 3.8
