"""
Unit tests for VLM post-conversion document enrichment pipeline (Phase 2).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from zocrates.rag.vlm_captioner import enrich_document_bundle


def _create_test_image(
    path: Path,
    width: int = 300,
    height: int = 300,
    min_size_bytes: int = 5000,
) -> None:
    """Create a valid PNG image of specified dimensions with minimum file size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(73, 109, 137))
    # Write image with custom metadata / compression to ensure it exceeds min_size_bytes
    img.save(path, format="PNG")
    # Pad file if needed to meet size constraint
    curr_size = path.stat().st_size
    if curr_size < min_size_bytes:
        with open(path, "ab") as f:
            f.write(b"\x00" * (min_size_bytes - curr_size + 100))


@pytest.fixture
def sample_bundle(tmp_path: Path):
    bundle_dir = tmp_path / "sample_item"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    images_dir = bundle_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    return bundle_dir


class TestVLMPipelineEnrichment:
    """Test suite for enrich_document_bundle."""

    def test_enrich_uncaptioned_figure_success(self, sample_bundle: Path):
        """Verify uncaptioned <figure> block receives <synthetic_caption> injection."""
        img_path = sample_bundle / "images" / "loss_curve.png"
        _create_test_image(img_path, width=400, height=300)

        initial_md = (
            "---\ntitle: Sample Paper\n---\n\n"
            "# Section 1\n\n"
            "<figure data-type=\"image\">\n"
            "![Figure 1](images/loss_curve.png)\n"
            "**Figure 1.** Training loss over 100 epochs.\n"
            "</figure>\n\n"
            "Some narrative text following the figure.\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="**Type**: Line Plot\n**Key Elements**: Epochs vs Loss\n**Takeaway**: Fast convergence."
                )
            )
        ]

        with patch("litellm.completion", return_value=mock_resp):
            enriched_md = enrich_document_bundle(
                sample_bundle,
                force_refresh=True,
            )

        assert "<synthetic_caption" in enriched_md
        assert 'data-source="vlm_remote"' in enriched_md
        assert "</synthetic_caption>" in enriched_md
        assert "**Type**: Line Plot" in enriched_md
        assert "**Figure 1.** Training loss over 100 epochs." in enriched_md
        assert "Some narrative text following the figure." in enriched_md

        # Check document.md on disk was updated
        saved_md = doc_path.read_text(encoding="utf-8")
        assert saved_md == enriched_md

        # Check sidecar was created
        sidecar = sample_bundle / "image_captions.json"
        assert sidecar.exists()
        with open(sidecar) as f:
            data = json.load(f)
            assert "images/loss_curve.png" in data["captions"]
            assert data["captions"]["images/loss_curve.png"]["source"] == "vlm_remote"

    def test_enrich_when_disabled_returns_unchanged(self, sample_bundle: Path):
        """Verify when VLM captioning is disabled, document is completely untouched."""
        img_path = sample_bundle / "images" / "fig1.png"
        _create_test_image(img_path)

        initial_md = (
            "<figure data-type=\"image\">\n"
            "![Figure 1](images/fig1.png)\n"
            "**Figure 1.** Training loss.\n"
            "</figure>\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        with patch("zocrates.rag.vlm_captioner.get_enable_vlm_captioning", return_value=False):
            with patch("litellm.completion") as mock_litellm:
                enriched_md = enrich_document_bundle(sample_bundle, force_refresh=False)
                mock_litellm.assert_not_called()

        assert enriched_md == initial_md
        assert not (sample_bundle / "image_captions.json").exists()

    def test_datalab_preexisting_caption_bypass(self, sample_bundle: Path):
        """Verify figures with existing <synthetic_caption> are skipped (0 VLM calls)."""
        img_path = sample_bundle / "images" / "datalab_fig.png"
        _create_test_image(img_path)

        initial_md = (
            "<figure data-type=\"image\">\n"
            "![Figure 1](images/datalab_fig.png)\n"
            "**Figure 1.** Dense OCR extracted chart.\n"
            "<synthetic_caption>\n"
            "**Type**: Scatter Plot\n"
            "**Key Elements**: Clusters A and B\n"
            "**Takeaway**: Linear separation.\n"
            "</synthetic_caption>\n"
            "</figure>\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        with patch("litellm.completion") as mock_litellm:
            enriched_md = enrich_document_bundle(sample_bundle, force_refresh=True)
            mock_litellm.assert_not_called()

        assert enriched_md == initial_md

    def test_enrich_standalone_image_wrapping(self, sample_bundle: Path):
        """Verify standalone markdown images are wrapped in <figure data-type="image"> with synthetic caption."""
        img_path = sample_bundle / "images" / "diagram.png"
        _create_test_image(img_path, width=500, height=400)

        initial_md = (
            "# System Architecture\n\n"
            "Here is the overall system flow:\n\n"
            "![System Architecture Diagram](images/diagram.png)\n\n"
            "As seen in the diagram above, the pipeline consists of 3 stages.\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="**Type**: Architecture Flowchart\n**Key Elements**: Stage 1 to 3\n**Takeaway**: Modular design."
                )
            )
        ]

        with patch("litellm.completion", return_value=mock_resp):
            enriched_md = enrich_document_bundle(sample_bundle, force_refresh=True)

        assert "<figure data-type=\"image\">" in enriched_md
        assert "![System Architecture Diagram](images/diagram.png)" in enriched_md
        assert "<synthetic_caption" in enriched_md
        assert 'data-source="vlm_remote"' in enriched_md
        assert "**Type**: Architecture Flowchart" in enriched_md
        assert "</synthetic_caption>" in enriched_md
        assert "</figure>" in enriched_md
        assert "As seen in the diagram above, the pipeline consists of 3 stages." in enriched_md

    def test_code_fence_protection(self, sample_bundle: Path):
        """Verify markdown image tags inside code blocks are not touched."""
        img_path = sample_bundle / "images" / "fake.png"
        _create_test_image(img_path)

        initial_md = (
            "```markdown\n"
            "![Code Sample](images/fake.png)\n"
            "```\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        with patch("litellm.completion") as mock_litellm:
            enriched_md = enrich_document_bundle(sample_bundle, force_refresh=True)
            mock_litellm.assert_not_called()

        assert enriched_md == initial_md

    def test_error_resilience_preserves_document(self, sample_bundle: Path):
        """Verify errors during VLM enrichment do not corrupt the original document."""
        img_path = sample_bundle / "images" / "broken.png"
        _create_test_image(img_path)

        initial_md = (
            "<figure data-type=\"image\">\n"
            "![Broken Fig](images/broken.png)\n"
            "</figure>\n"
        )
        doc_path = sample_bundle / "document.md"
        doc_path.write_text(initial_md, encoding="utf-8")

        with patch("litellm.completion", side_effect=RuntimeError("OpenRouter network failure")):
            enriched_md = enrich_document_bundle(sample_bundle, force_refresh=True)

        assert enriched_md == initial_md
        assert doc_path.read_text(encoding="utf-8") == initial_md

    def test_missing_document_returns_empty(self, tmp_path: Path):
        """Verify missing document.md returns empty string without crashing."""
        empty_dir = tmp_path / "non_existent"
        assert enrich_document_bundle(empty_dir) == ""
