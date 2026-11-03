"""
VLM Image Captioning & Multimodal Figure Enrichment Engine.

Provides automated visual description generation for uncaptioned diagrams,
plots, flowcharts, and academic figures using Vision-Language Models (VLMs)
via OpenRouter (Cloud) or Ollama (Local).
"""

import base64
import io
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import litellm
from PIL import Image, UnidentifiedImageError

try:
    import resvg_py
except ImportError:
    resvg_py = None

from zocrates.config import (
    DEFAULT_ENABLE_VLM_CAPTIONING,
    DEFAULT_VLM_CAPTIONING_BACKUP_MODEL,
    DEFAULT_VLM_CAPTIONING_LOCAL_MODEL,
    DEFAULT_VLM_CAPTIONING_MODEL,
    DEFAULT_VLM_CAPTIONING_PROVIDER,
    DEFAULT_VLM_MAX_IMAGES_PER_DOC,
    DEFAULT_VLM_MIN_FILE_SIZE_BYTES,
    DEFAULT_VLM_MIN_IMAGE_DIMENSION,
    DEFAULT_VLM_TIMEOUT_SECONDS,
    get_enable_vlm_captioning,
    get_secure_api_key,
    get_vlm_captioning_backup_model,
    get_vlm_captioning_local_model,
    get_vlm_captioning_model,
    get_vlm_captioning_provider,
    get_vlm_max_images_per_doc,
    get_vlm_timeout_seconds,
)

logger = logging.getLogger(__name__)

FIGURE_CAPTION_SYSTEM_PROMPT = """You are an expert academic document assistant. 
Analyze the provided figure or diagram from a research paper or document.
Provide a concise, dense, factual description (under 120 words) with the following structure:

- **Type**: Visualization or figure category (e.g., Line Plot, Architecture Flowchart, Confusion Matrix, Microscopy Panel, Circuit Schematic, Geographical Map, Manuscript Scan).
- **Key Elements**: Specific variables, axes (x and y labels with units), legends, mathematical notations, or chemical/biological structures visible.
- **Takeaway**: The primary scientific finding, trend, baseline comparison, or structural relationship depicted.

### Domain-Specific Points to Consider:
- **Computer Science & Engineering**: Note system architectures, inputs/outputs, network layers, benchmark baselines, latency/throughput tradeoffs, or flowchart decision logic.
- **Biomedical & Life Sciences**: Identify microscopy stain/modality, animal/cell model, protein/gene names, gel/blot bands, error bars (p-values, SD/SEM), and control vs. treatment conditions.
- **Physics & Chemistry**: Note chemical structures, reaction mechanisms, spectral peaks (wavelength/energy), coordinate frames, field lines, and physical units.
- **Mathematics & Quantitative Finance**: Capture LaTeX notation, matrix dimensions, state spaces, distributions, confidence intervals, and trendlines.
- **Humanities, Social Sciences & Archives**: Note geographical regions, timeline periods, demographic cohorts, archival source details, or survey sample sizes.

### Guardrails:
- If the image is a decorative icon, logo, author headshot, or unreadable banner, reply strictly with: [DECORATIVE].
- Do not hallucinate values or conclusions not directly visible in the image."""


def normalize_image_relative_path(image_ref: str, bundle_dir: Path) -> tuple[str, Path]:
    """Normalize an image reference (relative path or file:// URI) to relative and absolute paths.

    Args:
        image_ref: Raw path string from Markdown (e.g. 'images/fig1.png' or 'file:///path/to/fig1.png').
        bundle_dir: Item bundle directory.

    Returns:
        Tuple of (bundle_relative_path, absolute_path).
    """
    ref = image_ref.strip()
    bundle_resolved = bundle_dir.resolve()

    if ref.startswith("file://"):
        parsed = urlparse(ref)
        abs_path = Path(unquote(parsed.path)).resolve()
        if not abs_path.is_relative_to(bundle_resolved):
            logger.warning("Blocked path traversal or external file URI in image: %s", ref)
            return ref, bundle_resolved / "__traversal_blocked__"
        try:
            rel_path = abs_path.relative_to(bundle_resolved).as_posix()
        except ValueError:
            rel_path = f"images/{abs_path.name}"
        return rel_path, abs_path

    # Standard relative path
    clean_ref = ref.lstrip("/")
    abs_path = (bundle_dir / clean_ref).resolve()
    if not abs_path.is_relative_to(bundle_resolved):
        logger.warning("Blocked path traversal attempt in image ref: %s", ref)
        return clean_ref, bundle_resolved / "__traversal_blocked__"
    return clean_ref, abs_path


def file_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file in 64 KiB blocks."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class VLMCaptioner:
    """Vision-Language Model figure enrichment service.

    Filters uncaptioned figures, downscales them for prompt payload efficiency,
    and invokes OpenRouter or Ollama to generate structured academic captions.
    Persists results in sidecar cache `image_captions.json`.
    """

    def __init__(
        self,
        zotero_data_dir: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        backup_model: str | None = None,
        local_model: str | None = None,
        max_images_per_doc: int | None = None,
        min_dimension: int = DEFAULT_VLM_MIN_IMAGE_DIMENSION,
        min_file_size: int = DEFAULT_VLM_MIN_FILE_SIZE_BYTES,
        timeout: int | None = None,
        openrouter_api_key: str | None = None,
        ollama_host: str | None = None,
    ) -> None:
        """Initialize the VLM figure captioner service.

        Args:
            zotero_data_dir: Optional Zotero data directory path.
            provider: VLM provider name ('openrouter' or 'ollama').
            model: Primary VLM model identifier.
            backup_model: Fallback VLM model identifier for OpenRouter.
            local_model: Local Ollama model identifier.
            max_images_per_doc: Maximum number of images to caption per document.
            min_dimension: Minimum pixel width/height required to process an image.
            min_file_size: Minimum byte size required to process an image.
            timeout: Network request timeout in seconds.
            openrouter_api_key: Optional OpenRouter API key override.
            ollama_host: Optional Ollama base host URL.
        """
        self.zotero_data_dir = zotero_data_dir
        self.enabled = get_enable_vlm_captioning(zotero_data_dir)
        self.provider = (
            provider
            if provider is not None
            else get_vlm_captioning_provider(zotero_data_dir)
        )
        self.model = (
            model if model is not None else get_vlm_captioning_model(zotero_data_dir)
        )
        self.backup_model = (
            backup_model
            if backup_model is not None
            else get_vlm_captioning_backup_model(zotero_data_dir)
        )
        self.local_model = (
            local_model
            if local_model is not None
            else get_vlm_captioning_local_model(zotero_data_dir)
        )
        self.max_images_per_doc = (
            max_images_per_doc
            if max_images_per_doc is not None
            else get_vlm_max_images_per_doc(zotero_data_dir)
        )
        self.min_dimension = min_dimension
        self.min_file_size = min_file_size
        self.timeout = (
            timeout
            if timeout is not None
            else get_vlm_timeout_seconds(zotero_data_dir)
        )
        self.openrouter_api_key = (
            openrouter_api_key or get_secure_api_key("openrouter_api_key") or ""
        )
        self.ollama_host = (
            ollama_host
            or os.getenv("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")

    @staticmethod
    def get_sidecar_path(bundle_dir: Path) -> Path:
        """Return path to `image_captions.json` inside the item bundle."""
        return bundle_dir / "image_captions.json"

    def load_sidecar_captions(self, bundle_dir: Path) -> dict:
        """Load `image_captions.json` from bundle directory if present."""
        sidecar = self.get_sidecar_path(bundle_dir)
        if sidecar.exists():
            try:
                with open(sidecar, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "captions" in data:
                        return data
            except Exception as e:
                logger.warning(
                    "Failed to read image_captions.json from %s: %s", sidecar, e
                )
        return {"version": "1.0", "updated_at": 0.0, "captions": {}}

    def save_sidecar_captions(self, bundle_dir: Path, captions_data: dict) -> None:
        """Save `image_captions.json` into bundle directory."""
        sidecar = self.get_sidecar_path(bundle_dir)
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            with open(sidecar, "w", encoding="utf-8") as f:
                json.dump(captions_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(
                "Failed to write image_captions.json to %s: %s", sidecar, e
            )

    def filter_and_select_images(
        self, bundle_dir: Path, image_refs: list[str]
    ) -> list[tuple[str, Path]]:
        """Filter uncaptioned images by size, dimension, and aspect ratio.

        Args:
            bundle_dir: Item bundle directory.
            image_refs: Relative or absolute path strings found in Markdown.

        Returns:
            List of (rel_path, abs_path) tuples for selected images sorted by area descending,
            capped at `max_images_per_doc`.
        """
        candidates: list[tuple[int, str, Path]] = []
        seen_paths: set[Path] = set()

        for ref in image_refs:
            rel_path, abs_path = normalize_image_relative_path(ref, bundle_dir)
            if abs_path in seen_paths:
                continue
            seen_paths.add(abs_path)

            if not abs_path.exists() or not abs_path.is_file():
                continue

            try:
                file_size = abs_path.stat().st_size
                if file_size < self.min_file_size:
                    continue

                if abs_path.suffix.lower() == ".svg":
                    if not resvg_py:
                        logger.debug("Skipping SVG image %s (resvg-py not installed)", abs_path.name)
                        continue
                    svg_content = abs_path.read_text(encoding="utf-8", errors="ignore")
                    png_bytes = resvg_py.svg_to_bytes(svg_content)
                    with Image.open(io.BytesIO(png_bytes)) as img:
                        width, height = img.size
                else:
                    # Safely inspect dimensions with PIL without decoding full pixel buffer
                    with Image.open(abs_path) as img:
                        width, height = img.size

                if width < self.min_dimension or height < self.min_dimension:
                    continue

                # Filter extreme aspect ratios (divider lines, border strips)
                aspect_ratio = width / height
                if aspect_ratio < 0.05 or aspect_ratio > 20.0:
                    continue

                area = width * height
                candidates.append((area, rel_path, abs_path))
            except (UnidentifiedImageError, OSError, Exception) as e:
                logger.debug(
                    "Skipping unreadable or malformed image %s: %s", abs_path.name, e
                )
                continue

        # Sort by area descending (largest/most prominent figures first)
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = [(rel, path) for _, rel, path in candidates[: self.max_images_per_doc]]
        return selected

    def _encode_and_downscale_image(self, image_path: Path) -> tuple[str, str]:
        """Load, downscale to max 1024px if needed, and base64-encode image.

        Returns:
            Tuple of (mime_type, base64_string).
        """
        if image_path.suffix.lower() == ".svg" and resvg_py:
            svg_content = image_path.read_text(encoding="utf-8", errors="ignore")
            png_bytes = resvg_py.svg_to_bytes(svg_content)
            img_ctx = Image.open(io.BytesIO(png_bytes))
            orig_format = "PNG"
            mime_type = "image/png"
        else:
            mime_type, _ = mimetypes.guess_type(str(image_path))
            if not mime_type or not mime_type.startswith("image/"):
                mime_type = "image/png"
            img_ctx = Image.open(image_path)

        with img_ctx as img:
            # Handle palette/RGBA conversions if saving as JPEG
            orig_format = getattr(img, "format", None) or "PNG"
            width, height = img.size
            max_dim = max(width, height)

            if max_dim > 1024:
                scale = 1024.0 / max_dim
                new_width = max(1, int(width * scale))
                new_height = max(1, int(height * scale))
                img = img.resize(
                    (new_width, new_height),
                    resample=getattr(Image.Resampling, "LANCZOS", Image.LANCZOS),
                )

            buffer = io.BytesIO()
            if orig_format.upper() in ("JPEG", "JPG"):
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(buffer, format="JPEG", quality=85)
                mime_type = "image/jpeg"
            else:
                img.save(buffer, format="PNG", optimize=True)
                mime_type = "image/png"

            b64_str = base64.b64encode(buffer.getvalue()).decode("ascii")
            return mime_type, b64_str

    def _caption_with_openrouter(
        self,
        image_path: Path,
        api_key: str | None = None,
        disable_reasoning: bool = True,
    ) -> tuple[str | None, str]:
        """Invoke OpenRouter VLM with automatic primary-to-backup model fallback.

        Returns:
            Tuple of (caption_text_or_None, model_used).
        """
        effective_key = api_key or self.openrouter_api_key
        if not effective_key:
            logger.warning("OpenRouter API key is not configured for VLM captioning")
            return None, ""

        mime_type, b64_str = self._encode_and_downscale_image(image_path)
        messages = [
            {"role": "system", "content": FIGURE_CAPTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe this figure following the system guidelines.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_str}"},
                    },
                ],
            },
        ]

        models_to_try = [self.model]
        if self.backup_model and self.backup_model != self.model:
            models_to_try.append(self.backup_model)

        for current_model in models_to_try:
            try:
                call_kwargs = {
                    "model": current_model,
                    "messages": messages,
                    "api_key": effective_key,
                    "timeout": self.timeout,
                }
                if disable_reasoning:
                    call_kwargs["extra_body"] = {"reasoning": {"effort": "none"}}
                resp = litellm.completion(**call_kwargs)
                raw_text = (
                    resp.choices[0].message.content
                    if resp.choices and resp.choices[0].message
                    else ""
                )
                cleaned = (raw_text or "").strip()

                if "[DECORATIVE]" in cleaned.upper():
                    return None, current_model

                if cleaned:
                    return cleaned, current_model
            except Exception as e:
                logger.warning(
                    "VLM captioning failed with model %s on %s: %s",
                    current_model,
                    image_path.name,
                    e,
                )
                # Continue loop to attempt fallback model if available

        return None, ""

    def _caption_with_ollama(self, image_path: Path) -> tuple[str | None, str]:
        """Invoke local Ollama VLM.

        Returns:
            Tuple of (caption_text_or_None, model_used).
        """
        if not self.local_model:
            logger.warning("No local Ollama vision model configured")
            return None, ""

        import requests

        _, b64_str = self._encode_and_downscale_image(image_path)
        url = f"{self.ollama_host}/api/generate"
        payload = {
            "model": self.local_model,
            "prompt": (
                FIGURE_CAPTION_SYSTEM_PROMPT
                + "\n\nDescribe this figure following the system guidelines."
            ),
            "images": [b64_str],
            "stream": False,
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            raw_text = data.get("response", "").strip()

            if "[DECORATIVE]" in raw_text.upper():
                return None, self.local_model

            if raw_text:
                return raw_text, self.local_model
        except Exception as e:
            logger.warning(
                "Ollama VLM captioning failed with model %s on %s: %s",
                self.local_model,
                image_path.name,
                e,
            )

        return None, ""

    def caption_image(
        self,
        image_path: Path,
        api_key: str | None = None,
        disable_reasoning: bool = True,
    ) -> tuple[str | None, str, str]:
        """Generate structured academic caption for a single image.

        Returns:
            Tuple of (caption_text_or_None, model_used, caption_source_flag).
        """
        if self.provider == "ollama":
            caption, model = self._caption_with_ollama(image_path)
            source = "vlm_local"
        else:
            caption, model = self._caption_with_openrouter(
                image_path, api_key=api_key, disable_reasoning=disable_reasoning
            )
            source = "vlm_remote"

        return caption, model, source

    def enrich_bundle_images(
        self,
        bundle_dir: Path,
        image_refs: list[str],
        force_refresh: bool = False,
    ) -> dict[str, dict]:
        """Enrich uncaptioned figures in an item bundle with VLM captions.

        Checks the sidecar cache (`image_captions.json`) first. Uncached/modified
        images are filtered, downscaled, captioned, and added to the sidecar.

        Args:
            bundle_dir: Item bundle directory (`corpus/<item_key>/`).
            image_refs: Image paths or URIs discovered in `document.md`.
            force_refresh: If True, bypass sidecar cache and re-caption.

        Returns:
            Mapping of relative_image_path -> caption_dict.
        """
        sidecar_data = self.load_sidecar_captions(bundle_dir)
        captions: dict[str, dict] = dict(sidecar_data.get("captions", {}))
        dirty = False

        if not self.enabled and not force_refresh:
            return captions

        # Select candidate images to process
        selected_candidates = self.filter_and_select_images(bundle_dir, image_refs)

        for rel_path, abs_path in selected_candidates:
            current_hash = file_sha256(abs_path)

            # Check cache hit
            cached = captions.get(rel_path)
            if not force_refresh and cached and isinstance(cached, dict):
                if cached.get("sha256") == current_hash:
                    # Verified cache hit — skip VLM invocation
                    continue

            # Generate caption
            caption_text, model_used, source_flag = self.caption_image(abs_path)
            if caption_text:
                captions[rel_path] = {
                    "caption": caption_text,
                    "provider": self.provider,
                    "model": model_used,
                    "sha256": current_hash,
                    "source": source_flag,
                }
                dirty = True
            elif cached and cached.get("sha256") != current_hash:
                # File modified but now uncaptionable/decorative; purge stale caption
                captions.pop(rel_path, None)
                dirty = True

        if dirty:
            sidecar_data["captions"] = captions
            sidecar_data["updated_at"] = time.time()
            self.save_sidecar_captions(bundle_dir, sidecar_data)

        return captions


_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", re.MULTILINE)
_FIGURE_BLOCK_RE = re.compile(r"(<figure\b[^>]*>[\s\S]*?</figure>)", re.IGNORECASE)
_SYNTHETIC_CAPTION_RE = re.compile(r"<synthetic_caption\b", re.IGNORECASE)
_IMG_TAG_OR_MD_RE = re.compile(
    r"!\[(.*?)\]\((.*?)\)|<img\b[^>]*\bsrc=[\"'](.*?)[\"']", re.IGNORECASE
)
_MD_IMAGE_STANDALONE_RE = re.compile(r"!\[(.*?)\]\((.*?)\)")


def _mask_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code blocks with opaque placeholders."""
    masks = {}

    def replacer(m: re.Match) -> str:
        """Replace matched code fence with placeholder token.

        Args:
            m: Regex match object representing the code fence.

        Returns:
            Placeholder string token.
        """
        key = f"__VLM_CODE_BLOCK_{len(masks)}__"
        masks[key] = m.group(0)
        return key

    masked = _FENCE_RE.sub(replacer, text)
    return masked, masks


def _unmask_code_blocks(text: str, masks: dict[str, str]) -> str:
    """Restore fenced code blocks from their placeholder tokens."""
    result = text
    for key, orig in masks.items():
        result = result.replace(key, orig)
    return result


def enrich_document_bundle(
    bundle_dir: Path | str,
    zotero_data_dir: Path | None = None,
    force_refresh: bool = False,
) -> str:
    """Enrich uncaptioned figures in an item bundle Markdown with VLM descriptions.

    Scans `document.md` in `bundle_dir` for figure blocks and standalone image links.
    Figures with existing `<synthetic_caption>` (e.g. from Datalab OCR) are skipped.
    Generated visual descriptions are injected as `<synthetic_caption>` sections
    inside `<figure data-type="image">` blocks, and saved to `document.md`.

    Args:
        bundle_dir: Path to the item bundle directory (e.g. `corpus/<item_key>/`).
        zotero_data_dir: Optional root Zotero data directory for loading settings.
        force_refresh: If True, bypasses cache and regenerates captions.

    Returns:
        The enriched Markdown string (or original string on error/disabled).
    """
    b_dir = Path(bundle_dir)
    doc_path = b_dir / "document.md"

    if not doc_path.exists() or not doc_path.is_file():
        logger.debug("No document.md found in %s", b_dir)
        return ""

    try:
        content = doc_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read %s: %s", doc_path, e)
        return ""

    if not get_enable_vlm_captioning(zotero_data_dir) and not force_refresh:
        return content

    try:
        # Mask code fences so sample image tags inside code blocks are preserved
        masked_text, code_masks = _mask_code_blocks(content)

        # 1. Scan and mask <figure> blocks
        figure_masks: dict[str, tuple[str, list[str], bool]] = {}

        def figure_replacer(m: re.Match) -> str:
            """Mask a <figure> block and extract contained image references.

            Args:
                m: Regex match object representing the figure block.

            Returns:
                Placeholder string token.
            """
            key = f"__VLM_FIGURE_BLOCK_{len(figure_masks)}__"
            block_text = m.group(0)
            has_synth = bool(_SYNTHETIC_CAPTION_RE.search(block_text))
            img_refs = []
            if not has_synth:
                for img_m in _IMG_TAG_OR_MD_RE.finditer(block_text):
                    # Markdown image ref is group 2; HTML img src is group 3
                    raw_ref = (img_m.group(2) or img_m.group(3) or "").strip()
                    if raw_ref:
                        # Strip optional markdown title: ![alt](url "title")
                        ref_url = raw_ref.split()[0]
                        img_refs.append(ref_url)
            figure_masks[key] = (block_text, img_refs, has_synth)
            return key

        masked_text = _FIGURE_BLOCK_RE.sub(figure_replacer, masked_text)

        # 2. Find standalone markdown images outside <figure> blocks
        standalone_img_refs: list[str] = []
        for img_m in _MD_IMAGE_STANDALONE_RE.finditer(masked_text):
            raw_ref = (img_m.group(2) or "").strip()
            if raw_ref:
                ref_url = raw_ref.split()[0]
                standalone_img_refs.append(ref_url)

        # 3. Collect all candidate uncaptioned image references
        all_candidate_refs: list[str] = []
        for _, refs, has_synth in figure_masks.values():
            if not has_synth:
                all_candidate_refs.extend(refs)
        all_candidate_refs.extend(standalone_img_refs)

        if not all_candidate_refs:
            return content

        # 4. Invoke VLMCaptioner
        captioner = VLMCaptioner(zotero_data_dir=zotero_data_dir)
        captions = captioner.enrich_bundle_images(
            b_dir, all_candidate_refs, force_refresh=force_refresh
        )

        if not captions:
            return content

        # 5. Enrich standalone images by wrapping in <figure data-type="image">
        def standalone_img_replacer(m: re.Match) -> str:
            """Wrap an unencapsulated markdown image in a <figure> tag with synthetic caption.

            Args:
                m: Regex match object representing the markdown image.

            Returns:
                Enriched figure block or original markdown image.
            """
            full_md = m.group(0)
            raw_ref = (m.group(2) or "").strip()
            if not raw_ref:
                return full_md
            ref_url = raw_ref.split()[0]
            rel_path, _ = normalize_image_relative_path(ref_url, b_dir)
            cap_entry = captions.get(rel_path)
            if cap_entry and cap_entry.get("caption"):
                caption_text = cap_entry["caption"].strip()
                source_attr = f' data-source="{cap_entry.get("source") or "vlm_remote"}"'
                model_attr = f' data-model="{cap_entry.get("model")}"' if cap_entry.get("model") else ""
                return (
                    f'<figure data-type="image">\n'
                    f"{full_md}\n"
                    f"<synthetic_caption{source_attr}{model_attr}>\n"
                    f"{caption_text}\n"
                    f"</synthetic_caption>\n"
                    f"</figure>"
                )
            return full_md

        masked_text = _MD_IMAGE_STANDALONE_RE.sub(standalone_img_replacer, masked_text)

        # 6. Enrich <figure> blocks that got captions
        for key, (orig_block, refs, has_synth) in figure_masks.items():
            if has_synth or not refs:
                masked_text = masked_text.replace(key, orig_block)
                continue

            # Find matching caption for any image in this figure
            matching_caption = None
            matching_cap_entry = None
            for ref in refs:
                rel_path, _ = normalize_image_relative_path(ref, b_dir)
                cap_entry = captions.get(rel_path)
                if cap_entry and cap_entry.get("caption"):
                    matching_caption = cap_entry["caption"].strip()
                    matching_cap_entry = cap_entry
                    break

            if matching_caption:
                source_attr = f' data-source="{matching_cap_entry.get("source") or "vlm_remote"}"'
                model_attr = f' data-model="{matching_cap_entry.get("model")}"' if matching_cap_entry.get("model") else ""
                synth_tag_open = f"<synthetic_caption{source_attr}{model_attr}>"

                closing_m = re.search(r"</figure\s*>$", orig_block, re.IGNORECASE)
                if closing_m:
                    prefix = orig_block[:closing_m.start()].rstrip()
                    new_block = (
                        f"{prefix}\n"
                        f"{synth_tag_open}\n"
                        f"{matching_caption}\n"
                        f"</synthetic_caption>\n"
                        f"</figure>"
                    )
                else:
                    new_block = (
                        f"{orig_block}\n"
                        f"{synth_tag_open}\n"
                        f"{matching_caption}\n"
                        f"</synthetic_caption>"
                    )
                masked_text = masked_text.replace(key, new_block)
            else:
                masked_text = masked_text.replace(key, orig_block)

        # 7. Unmask code fences
        enriched_content = _unmask_code_blocks(masked_text, code_masks)

        if enriched_content != content:
            doc_path.write_text(enriched_content, encoding="utf-8")
            logger.info("Enriched %s with VLM figure captions", doc_path.name)
            return enriched_content

        return content

    except Exception as e:
        logger.warning(
            "VLM figure enrichment encountered an error on %s: %s; returning original markdown",
            b_dir.name,
            e,
            exc_info=True,
        )
        return content

