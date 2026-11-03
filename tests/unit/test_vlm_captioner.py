"""
Unit tests for VLMCaptioner (Phase 1).
"""

import base64
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from zocrates.config import (
    DEFAULT_ENABLE_VLM_CAPTIONING,
    DEFAULT_VLM_CAPTIONING_BACKUP_MODEL,
    DEFAULT_VLM_CAPTIONING_MODEL,
    DEFAULT_VLM_CAPTIONING_PROVIDER,
    DEFAULT_VLM_MAX_IMAGES_PER_DOC,
    DEFAULT_VLM_MIN_FILE_SIZE_BYTES,
    DEFAULT_VLM_MIN_IMAGE_DIMENSION,
    DEFAULT_VLM_TIMEOUT_SECONDS,
    get_enable_vlm_captioning,
    get_vlm_captioning_backup_model,
    get_vlm_captioning_local_model,
    get_vlm_captioning_model,
    get_vlm_captioning_provider,
    get_vlm_max_images_per_doc,
    get_vlm_timeout_seconds,
)
from zocrates.rag.vlm_captioner import (
    FIGURE_CAPTION_SYSTEM_PROMPT,
    VLMCaptioner,
    file_sha256,
    normalize_image_relative_path,
)


def _create_test_image(
    path: Path,
    width: int = 300,
    height: int = 300,
    format: str = "PNG",
    color: tuple = (255, 0, 0),
    min_size_bytes: int | None = None,
) -> Path:
    """Helper to create test image files of specific dimensions and minimum byte size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    buffer = io.BytesIO()
    img.save(buffer, format=format)
    data = buffer.getvalue()

    if min_size_bytes and len(data) < min_size_bytes:
        # Pad with metadata/trailing bytes or save with uncompressed raw data
        # To make valid PNG file size >= min_size_bytes, we can create larger random noise image
        import numpy as np

        noise = np.random.randint(0, 256, (width, height, 3), dtype=np.uint8)
        img = Image.fromarray(noise)
        buffer = io.BytesIO()
        img.save(buffer, format=format)
        data = buffer.getvalue()

    with open(path, "wb") as f:
        f.write(data)
    return path


class TestConfigGetters:
    def test_default_constants(self):
        assert DEFAULT_ENABLE_VLM_CAPTIONING is True
        assert DEFAULT_VLM_CAPTIONING_PROVIDER == "openrouter"
        # Phase 7 default alignment (§5.2): paid vision-capable primary
        assert "deepseek-v4.1-flash" in DEFAULT_VLM_CAPTIONING_MODEL
        assert "nemotron-3-nano-omni" in DEFAULT_VLM_CAPTIONING_BACKUP_MODEL
        assert DEFAULT_VLM_MAX_IMAGES_PER_DOC == 100
        assert DEFAULT_VLM_MIN_IMAGE_DIMENSION == 120
        assert DEFAULT_VLM_MIN_FILE_SIZE_BYTES == 4096
        assert DEFAULT_VLM_TIMEOUT_SECONDS == 30

    def test_getters_return_defaults(self, tmp_path):
        assert get_enable_vlm_captioning(tmp_path) is True
        assert get_vlm_captioning_provider(tmp_path) == "openrouter"
        assert "deepseek-v4.1-flash" in get_vlm_captioning_model(tmp_path)
        assert "nemotron-3-nano-omni" in get_vlm_captioning_backup_model(tmp_path)
        assert get_vlm_captioning_local_model(tmp_path) == ""
        assert get_vlm_max_images_per_doc(tmp_path) == 100
        assert get_vlm_timeout_seconds(tmp_path) == 30

    def test_getters_respect_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ENABLE_VLM_CAPTIONING", "false")
        monkeypatch.setenv("VLM_CAPTIONING_PROVIDER", "ollama")
        monkeypatch.setenv("VLM_CAPTIONING_MODEL", "custom/cloud-model")
        monkeypatch.setenv("VLM_CAPTIONING_BACKUP_MODEL", "custom/backup-model")
        monkeypatch.setenv("VLM_CAPTIONING_LOCAL_MODEL", "llava:13b")
        monkeypatch.setenv("VLM_MAX_IMAGES_PER_DOC", "15")
        monkeypatch.setenv("VLM_TIMEOUT_SECONDS", "45")

        assert get_enable_vlm_captioning(tmp_path) is False
        assert get_vlm_captioning_provider(tmp_path) == "ollama"
        assert get_vlm_captioning_model(tmp_path) == "custom/cloud-model"
        assert get_vlm_captioning_backup_model(tmp_path) == "custom/backup-model"
        assert get_vlm_captioning_local_model(tmp_path) == "llava:13b"
        assert get_vlm_max_images_per_doc(tmp_path) == 15
        assert get_vlm_timeout_seconds(tmp_path) == 45


class TestVLMCaptionerFilteringAndEncoding:
    def test_normalize_image_relative_path(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        # Relative path
        rel, abs_path = normalize_image_relative_path("images/fig1.png", bundle_dir)
        assert rel == "images/fig1.png"
        assert abs_path == (bundle_dir / "images/fig1.png").resolve()

        # file:// URI
        target = bundle_dir / "images" / "fig2.png"
        file_uri = target.as_uri()
        rel_uri, abs_uri = normalize_image_relative_path(file_uri, bundle_dir)
        assert rel_uri == "images/fig2.png"
        assert abs_uri == target.resolve()

    def test_file_sha256(self, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("zocrates test hashing")
        h1 = file_sha256(f)
        assert isinstance(h1, str)
        assert len(h1) == 64

    def test_filter_and_select_images_valid_and_invalid(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True)

        captioner = VLMCaptioner(min_file_size=100, min_dimension=120, max_images_per_doc=2)

        # 1. Valid large image (400x400)
        img_valid = _create_test_image(images_dir / "fig1.png", width=400, height=400)

        # 2. Valid medium image (200x200)
        img_valid2 = _create_test_image(images_dir / "fig2.png", width=200, height=200)

        # 3. Too small dimensions (80x80)
        _create_test_image(images_dir / "small_icon.png", width=80, height=80)

        # 4. Extreme aspect ratio (600x10, ratio 60)
        _create_test_image(images_dir / "divider.png", width=600, height=10)

        # 5. Non-image / SVG / Corrupted file
        corrupt = images_dir / "bad.png"
        corrupt.write_text("<svg>corrupt not a png</svg>")

        # 6. Missing file
        missing_ref = "images/missing.png"

        image_refs = [
            "images/fig1.png",
            "images/fig2.png",
            "images/small_icon.png",
            "images/divider.png",
            "images/bad.png",
            missing_ref,
        ]

        selected = captioner.filter_and_select_images(bundle_dir, image_refs)

        # Should select fig1 and fig2, sorted by area descending
        assert len(selected) == 2
        assert selected[0][0] == "images/fig1.png"
        assert selected[1][0] == "images/fig2.png"

    def test_encode_and_downscale_image(self, tmp_path):
        captioner = VLMCaptioner()
        img_path = tmp_path / "large_fig.png"
        # 2048 x 1024 -> should downscale to max 1024 on long edge -> 1024 x 512
        _create_test_image(img_path, width=2048, height=1024)

        mime_type, b64_str = captioner._encode_and_downscale_image(img_path)
        assert mime_type == "image/png"
        assert isinstance(b64_str, str)
        assert len(b64_str) > 0

        # Decode and verify downscaled dimensions
        import base64

        decoded_bytes = base64.b64decode(b64_str)
        with Image.open(io.BytesIO(decoded_bytes)) as decoded_img:
            assert decoded_img.width <= 1024
            assert decoded_img.height <= 1024
            assert decoded_img.width == 1024
            assert decoded_img.height == 512


class TestSidecarCaching:
    def test_load_and_save_sidecar(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        captioner = VLMCaptioner()

        # Load when missing returns default
        initial = captioner.load_sidecar_captions(bundle_dir)
        assert initial == {"version": "1.0", "updated_at": 0.0, "captions": {}}

        # Save data
        data = {
            "version": "1.0",
            "updated_at": 1755000000.0,
            "captions": {
                "images/fig1.png": {
                    "caption": "**Type**: Line Plot\n**Key Elements**: Epochs vs Loss\n**Takeaway**: Converges fast.",
                    "provider": "openrouter",
                    "model": "openrouter/google/gemma-4-26b-a4b-it:free",
                    "sha256": "abcdef123456",
                    "source": "vlm_remote",
                }
            },
        }
        captioner.save_sidecar_captions(bundle_dir, data)

        # Verify on disk
        sidecar_file = bundle_dir / "image_captions.json"
        assert sidecar_file.exists()

        # Load back
        reloaded = captioner.load_sidecar_captions(bundle_dir)
        assert reloaded["captions"]["images/fig1.png"]["caption"].startswith("**Type**: Line Plot")
        assert reloaded["captions"]["images/fig1.png"]["source"] == "vlm_remote"


class TestOpenRouterAndOllamaCaptioning:
    @patch("litellm.completion")
    def test_openrouter_success(self, mock_completion, tmp_path):
        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="**Type**: Bar Chart\n**Key Elements**: Accuracy (%)\n**Takeaway**: Outperforms baseline."
                )
            )
        ]
        mock_completion.return_value = mock_resp

        captioner = VLMCaptioner(openrouter_api_key="sk-test-key")
        img_path = _create_test_image(tmp_path / "fig.png", width=300, height=300)

        caption, model_used = captioner._caption_with_openrouter(img_path)
        assert caption is not None
        assert "**Type**: Bar Chart" in caption
        assert model_used == captioner.model
        assert mock_completion.call_count == 1

    @patch("litellm.completion")
    def test_openrouter_decorative_token_filtered(self, mock_completion, tmp_path):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="[DECORATIVE]"))]
        mock_completion.return_value = mock_resp

        captioner = VLMCaptioner(openrouter_api_key="sk-test-key")
        img_path = _create_test_image(tmp_path / "logo.png", width=300, height=300)

        caption, model_used = captioner._caption_with_openrouter(img_path)
        assert caption is None
        assert model_used == captioner.model

    @patch("litellm.completion")
    def test_openrouter_primary_failure_fallback_success(self, mock_completion, tmp_path):
        # First call (primary) raises Exception; second call (backup) succeeds
        mock_success_resp = MagicMock()
        mock_success_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="**Type**: Architecture Diagram\n**Key Elements**: Encoder, Decoder\n**Takeaway**: Transformer pipeline."
                )
            )
        ]
        mock_completion.side_effect = [
            Exception("Rate limit 429"),
            mock_success_resp,
        ]

        captioner = VLMCaptioner(
            openrouter_api_key="sk-test-key",
            model="openrouter/google/gemma-4-26b-a4b-it:free",
            backup_model="openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        )
        img_path = _create_test_image(tmp_path / "fig.png", width=300, height=300)

        caption, model_used = captioner._caption_with_openrouter(img_path)
        assert caption is not None
        assert "**Type**: Architecture Diagram" in caption
        assert model_used == "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
        assert mock_completion.call_count == 2

    @patch("litellm.completion")
    def test_openrouter_both_models_fail(self, mock_completion, tmp_path):
        mock_completion.side_effect = [
            Exception("Primary 500 error"),
            Exception("Backup 503 error"),
        ]

        captioner = VLMCaptioner(
            openrouter_api_key="sk-test-key",
            model="openrouter/primary",
            backup_model="openrouter/backup",
        )
        img_path = _create_test_image(tmp_path / "fig.png", width=300, height=300)

        caption, model_used = captioner._caption_with_openrouter(img_path)
        assert caption is None
        assert model_used == ""

    @patch("requests.post")
    def test_ollama_captioning_success(self, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "response": "**Type**: Confusion Matrix\n**Key Elements**: True vs Predicted classes\n**Takeaway**: 94% precision."
        }
        mock_post.return_value = mock_resp

        captioner = VLMCaptioner(
            provider="ollama",
            local_model="llava:7b",
            ollama_host="http://localhost:11434",
        )
        img_path = _create_test_image(tmp_path / "matrix.png", width=300, height=300)

        caption, model, source = captioner.caption_image(img_path)
        assert caption is not None
        assert "**Type**: Confusion Matrix" in caption
        assert model == "llava:7b"
        assert source == "vlm_local"
        assert mock_post.call_count == 1


class TestEnrichBundleImagesE2E:
    @patch.object(VLMCaptioner, "caption_image")
    def test_enrich_bundle_images_caching_and_enrichment(self, mock_caption, tmp_path):
        mock_caption.return_value = (
            "**Type**: Scatter Plot\n**Key Elements**: Feature A vs B\n**Takeaway**: Linear separation.",
            "openrouter/google/gemma-4-26b-a4b-it:free",
            "vlm_remote",
        )

        bundle_dir = tmp_path / "bundle"
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True)

        img_path = _create_test_image(images_dir / "plot1.png", width=350, height=350)
        image_refs = ["images/plot1.png"]

        captioner = VLMCaptioner(min_file_size=100, min_dimension=120)
        captioner.enabled = True

        # 1. First run: generates caption and writes sidecar
        captions_1 = captioner.enrich_bundle_images(bundle_dir, image_refs)
        assert "images/plot1.png" in captions_1
        assert captions_1["images/plot1.png"]["source"] == "vlm_remote"
        assert mock_caption.call_count == 1

        # Sidecar file exists
        assert (bundle_dir / "image_captions.json").exists()

        # 2. Second run: identical hash => 0 VLM calls (cache hit)
        captions_2 = captioner.enrich_bundle_images(bundle_dir, image_refs)
        assert "images/plot1.png" in captions_2
        assert captions_2["images/plot1.png"]["caption"] == captions_1["images/plot1.png"]["caption"]
        assert mock_caption.call_count == 1  # Still 1 call!

        # 3. File modified -> hash changes -> re-captions
        _create_test_image(img_path, width=350, height=350, color=(0, 255, 0))
        mock_caption.return_value = (
            "**Type**: Updated Scatter Plot\n**Key Elements**: New clusters\n**Takeaway**: Non-linear separation.",
            "openrouter/google/gemma-4-26b-a4b-it:free",
            "vlm_remote",
        )
        captions_3 = captioner.enrich_bundle_images(bundle_dir, image_refs)
        assert "Updated Scatter Plot" in captions_3["images/plot1.png"]["caption"]
        assert mock_caption.call_count == 2


class TestSVGRasterization:
    def test_filter_and_select_svg_diagram(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True)

        svg_content = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300">
          <rect width="100%" height="100%" fill="#f0f0f0"/>
          <circle cx="200" cy="150" r="80" fill="#3b82f6"/>
          <text x="200" y="160" font-size="24" text-anchor="middle" fill="white">SVG Diagram</text>
        </svg>"""
        # Ensure file size >= 100 bytes
        svg_file = images_dir / "diagram.svg"
        svg_file.write_text(svg_content + "\n<!-- " + "padding" * 500 + " -->", encoding="utf-8")

        captioner = VLMCaptioner(min_file_size=100, min_dimension=120)
        selected = captioner.filter_and_select_images(bundle_dir, ["images/diagram.svg"])

        assert len(selected) == 1
        assert selected[0][0] == "images/diagram.svg"
        assert selected[0][1] == svg_file

    def test_encode_and_downscale_svg_image(self, tmp_path):
        svg_content = """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="900" viewBox="0 0 1200 900">
          <rect width="100%" height="100%" fill="#ffffff"/>
          <text x="600" y="450" font-size="48" text-anchor="middle" fill="black">High-Res Vector Plot</text>
        </svg>"""
        svg_file = tmp_path / "vector_plot.svg"
        svg_file.write_text(svg_content, encoding="utf-8")

        captioner = VLMCaptioner()
        mime_type, b64_str = captioner._encode_and_downscale_image(svg_file)

        assert mime_type == "image/png"
        assert len(b64_str) > 100

        # Decode and verify dimensions were downscaled to max 1024px
        decoded_bytes = base64.b64decode(b64_str)
        with Image.open(io.BytesIO(decoded_bytes)) as img:
            assert max(img.size) <= 1024
            assert img.format == "PNG"

