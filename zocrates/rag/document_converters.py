"""Document converters module: provides modular converter strategies for Zocrates document processing.

Supports pluggable converter implementations adhering to `BaseDocumentConverter`:
  - `DirectReadConverter`: High-speed UTF-8 disk reader for Markdown (.md) and plain text (.txt) attachments (<1ms).
  - `MarkItDownConverter`: Universal document conversion engine via Microsoft MarkItDown,
    with page-by-page PDF image extraction via system `pdfimages`.
"""

import base64
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, NavigableString

if TYPE_CHECKING:
    from markitdown import MarkItDown

logger = logging.getLogger(__name__)

VC_REDIST_WINDOWS_MSG = (
    "Document conversion failed: Microsoft Visual C++ 2015–2022 Redistributable (x64) is required on Windows. "
    "Please install it from https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170#latest-supported-redistributable-version and restart Zotero."
)


def _is_vc_redist_dll_failure(exc: Exception) -> bool:
    """Return whether the exception is due to missing MSVC runtime DLLs (e.g. onnxruntime/magika)."""
    msg = str(exc)
    return (
        "DLL load failed" in msg
        or "onnxruntime_pybind11_state" in msg
        or "vcruntime140" in msg.lower()
        or "msvcp140" in msg.lower()
    )



def _get_package_version(package_name: str, fallback: str = "1.0.0") -> str:
    """Return the installed version string for a package name via importlib.metadata.

    Args:
        package_name: PyPI distribution name (e.g. 'markitdown').
        fallback: Fallback version string if the package metadata is missing.

    Returns:
        Package version string.
    """
    try:
        return importlib.metadata.version(package_name)
    except Exception:
        return fallback


def _extract_pdf_images(pdf_path: Path, output_dir: Path) -> dict[int, list[Path]]:
    """Extract all images from a PDF and map them to their respective 1-indexed page numbers.

    Uses system tool `pdfimages` (from poppler-utils) if available, with a Python-native
    fallback via `pypdf` when running in sandboxed environments like Flatpak.

    Args:
        pdf_path: Absolute path to the source PDF file.
        output_dir: Output directory where extracted PNG files will be stored.

    Returns:
        Dictionary mapping 1-indexed page number to list of absolute Path objects of extracted images.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    images_by_page: dict[int, list[Path]] = {}

    if shutil.which("pdfimages") is None:
        logger.info(
            "pdfimages command line tool not found. Falling back to pypdf image extraction on %s.",
            pdf_path.name,
        )
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            total_extracted = 0
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    for img_idx, img_file in enumerate(page.images):
                        try:
                            raw_name = getattr(img_file, "name", "") or ""
                            ext = Path(raw_name).suffix.lower() or ".png"
                            if not ext.startswith("."):
                                ext = f".{ext}"
                            img_path = output_dir / f"img_p{page_num}_{img_idx}{ext}"

                            if getattr(img_file, "data", None):
                                img_path.write_bytes(img_file.data)
                            elif getattr(img_file, "image", None):
                                img_file.image.save(img_path)
                            else:
                                continue

                            images_by_page.setdefault(page_num, []).append(img_path.resolve())
                            total_extracted += 1
                        except Exception as img_exc:
                            logger.debug(
                                "Failed extracting image %d on page %d of %s: %s",
                                img_idx,
                                page_num,
                                pdf_path.name,
                                img_exc,
                            )
                except Exception as page_exc:
                    logger.debug(
                        "Failed accessing images on page %d of %s: %s",
                        page_num,
                        pdf_path.name,
                        page_exc,
                    )
            logger.info("Extracted %d images via pypdf from %s", total_extracted, pdf_path.name)
        except Exception as exc:
            logger.error(
                "Failed to extract images from PDF %s via pypdf fallback: %s",
                pdf_path,
                exc,
            )
        return images_by_page

    try:
        # Step 1: List all images and their pages
        list_cmd = ["pdfimages", "-list", str(pdf_path)]
        res = subprocess.run(list_cmd, capture_output=True, text=True, check=True)

        # Parse the output table of pdfimages -list
        # Output has header lines followed by rows:
        # page   num  type   width  height  color  comp  bpc  enc   interp  object ID
        #    1     0  image    720     960  rgb       3    8  jpeg     no        9  0
        lines = res.stdout.strip().split("\n")
        image_mappings: list[tuple[int, int]] = []  # (page, num)

        for line in lines:
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            try:
                page_num = int(parts[0])
                image_idx = int(parts[1])
                image_mappings.append((page_num, image_idx))
            except (ValueError, IndexError):
                continue

        if not image_mappings:
            return {}

        # Step 2: Extract all images as PNG files
        # pdfimages puts files in output_dir prefixed with "img"
        prefix = output_dir / "img"
        extract_cmd = ["pdfimages", "-png", str(pdf_path), str(prefix)]
        subprocess.run(extract_cmd, check=True)

        # Step 3: Match extracted files to their pages
        # Extracted files are named img-000.png, img-001.png, etc. (alphabetic match)
        extracted_files = sorted(output_dir.glob("img-*.png"))

        for file_path, (page_num, _) in zip(
            extracted_files, image_mappings, strict=False
        ):
            images_by_page.setdefault(page_num, []).append(file_path.resolve())

        logger.info("Extracted %d images from %s", len(extracted_files), pdf_path.name)
    except Exception as exc:
        logger.error("Failed to extract images from PDF %s: %s", pdf_path, exc)

    return images_by_page


def _extract_html_images(
    source_path: Path, output_dir: Path
) -> list[tuple[int, Path, str | None]]:
    """Extract embedded base64 data URI and local relative images from an HTML document.

    Saves images to output_dir (e.g. images/picture_000.png, images/picture_001.jpg)
    and returns a list of (index, image_path, alt_text).

    Args:
        source_path: Path to the HTML document.
        output_dir: Target directory to store extracted image files.

    Returns:
        List of tuples: (image_index, file_path, alt_text).
    """
    if not source_path.exists():
        return []

    try:
        html_text = source_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Could not read HTML file %s for image extraction: %s", source_path, exc)
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    img_tags = soup.find_all("img")
    if not img_tags:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    saved_images: list[tuple[int, Path, str | None]] = []

    for idx, img in enumerate(img_tags):
        src = (img.get("src") or img.get("data-src") or "").strip()
        alt = (img.get("alt") or "").strip() or None
        if not src:
            continue

        if src.startswith("data:image/"):
            try:
                header, data = src.split(",", 1)
                mime = header.split(";")[0].replace("data:", "").lower()
                ext = ".png"
                if "jpeg" in mime or "jpg" in mime:
                    ext = ".jpg"
                elif "gif" in mime:
                    ext = ".gif"
                elif "webp" in mime:
                    ext = ".webp"
                elif "svg" in mime:
                    ext = ".svg"

                img_bytes = base64.b64decode(data.strip())
                dest_file = output_dir / f"picture_{idx:03d}{ext}"
                dest_file.write_bytes(img_bytes)
                saved_images.append((idx, dest_file.resolve(), alt))
            except Exception as exc:
                logger.debug("Failed decoding base64 image %d in %s: %s", idx, source_path.name, exc)
        elif not (src.startswith("http://") or src.startswith("https://") or src.startswith("//")):
            try:
                import urllib.parse
                unquoted_src = urllib.parse.unquote(src)
                resolved_parent = source_path.parent.resolve()
                resolved_local = (source_path.parent / unquoted_src).resolve()
                if not resolved_local.is_relative_to(resolved_parent):
                    logger.warning("Blocked path traversal attempt in HTML image: %s in %s", src, source_path.name)
                    continue
                ext = resolved_local.suffix.lower()
                if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}:
                    logger.debug("Skipping non-image file in HTML image tag: %s", src)
                    continue
                if resolved_local.is_file():
                    dest_file = output_dir / f"picture_{idx:03d}{ext}"
                    shutil.copy2(resolved_local, dest_file)
                    saved_images.append((idx, dest_file.resolve(), alt))
            except Exception as exc:
                logger.debug("Failed copying local image %d from %s: %s", idx, src, exc)

    logger.info("Extracted %d images from HTML %s", len(saved_images), source_path.name)
    return saved_images


def _inject_html_images_into_markdown(
    markdown_text: str,
    html_images: list[tuple[int, Path, str | None]],
    canonical_dir: Path | None = None,
) -> str:
    """Replace raw data:image/... and local image references with canonical <figure data-type="image"> blocks.

    Args:
        markdown_text: Converted Markdown text from MarkItDown.
        html_images: List of tuples (image_index, file_path, alt_text) from _extract_html_images.
        canonical_dir: Optional canonical directory for API compatibility.

    Returns:
        Markdown with canonical <figure data-type="image"> containers.
    """
    if not html_images:
        return markdown_text

    # Unwrap markdown links containing only images so adjacent images are individually addressable.
    # Pattern uses leading whitespace per-item plus trailing whitespace before closing bracket
    # to avoid ambiguous overlapping whitespace repetitions.
    markdown_text = re.sub(
        r'\[((?:\s*!\[[^\]]*\]\([^)]+\))+\s*)\]\([^)]+\)', r'\1', markdown_text
    )

    pattern = re.compile(
        r'\[\s*!\[([^\]]*)\]\(((?:data:image/[^)]+|(?!(?:https?|file):)[^)]+))\)\s*\]\([^)]+\)|!\[([^\]]*)\]\(((?:data:image/[^)]+|(?!(?:https?|file):)[^)]+))\)'
    )

    matches = list(pattern.finditer(markdown_text))
    if not matches:
        figures = []
        for idx, img_path, alt in html_images:
            rel_path = f"images/{img_path.name}"
            clean_alt = (alt or "").strip()
            if "data:image/" in clean_alt or clean_alt.startswith("data:") or "](" in clean_alt:
                clean_alt = ""
            alt_str = clean_alt or f"Figure {idx + 1}"
            caption_line = f"**Figure {idx + 1}.** {alt_str}" if not alt_str.lower().startswith("fig") else alt_str
            cap_str = f"\n\n{caption_line}\n" if clean_alt else "\n"
            fig_tag = f'<figure data-type="image" data-id="figure-{idx:03d}">\n\n![{alt_str}]({rel_path}){cap_str}</figure>'
            figures.append("\n\n" + fig_tag)
        return markdown_text + "".join(figures)

    result = []
    last_end = 0
    for idx, match in enumerate(matches):
        result.append(markdown_text[last_end : match.start()])
        last_end = match.end()
        if idx < len(html_images):
            pic_idx, img_path, alt_from_html = html_images[idx]
            rel_path = f"images/{img_path.name}"
            raw_alt = (match.group(1) if match.group(1) is not None else match.group(3)) or ""
            raw_alt = raw_alt.strip()
            if "data:image/" in raw_alt or raw_alt.startswith("data:") or "](" in raw_alt:
                raw_alt = ""

            clean_html_alt = (alt_from_html or "").strip()
            if "data:image/" in clean_html_alt or clean_html_alt.startswith("data:") or "](" in clean_html_alt:
                clean_html_alt = ""

            alt_str = raw_alt or clean_html_alt or f"Figure {pic_idx + 1}"
            if "data:image/" in alt_str or alt_str.startswith("data:") or "](" in alt_str:
                alt_str = f"Figure {pic_idx + 1}"

            caption_line = f"**Figure {pic_idx + 1}.** {alt_str}" if not alt_str.lower().startswith("fig") else alt_str
            cap_str = f"\n\n{caption_line}\n" if (raw_alt or clean_html_alt) else "\n"
            fig_tag = f'<figure data-type="image" data-id="figure-{pic_idx:03d}">\n\n![{alt_str}]({rel_path}){cap_str}</figure>'
            result.append(fig_tag)
        else:
            result.append(match.group(0))
    result.append(markdown_text[last_end:])

    if len(html_images) > len(matches):
        for pic_idx, img_path, alt in html_images[len(matches) :]:
            rel_path = f"images/{img_path.name}"
            clean_alt = (alt or "").strip()
            if "data:image/" in clean_alt or clean_alt.startswith("data:") or "](" in clean_alt:
                clean_alt = ""
            alt_str = clean_alt or f"Figure {pic_idx + 1}"
            caption_line = f"**Figure {pic_idx + 1}.** {alt_str}" if not alt_str.lower().startswith("fig") else alt_str
            cap_str = f"\n\n{caption_line}\n" if clean_alt else "\n"
            fig_tag = f'\n\n<figure data-type="image" data-id="figure-{pic_idx:03d}">\n\n![{alt_str}]({rel_path}){cap_str}</figure>'
            result.append(fig_tag)

    return "".join(result)


def _embed_images_in_markdown(
    markdown_text: str, images_by_page: dict[int, list[Path]]
) -> str:
    """Insert Markdown image tags into converted text based on page numbers.

    Splits text by form feed characters (`\\x0c`, standard page breaks). If text has only 1 page
    but images belong to higher page numbers, appends image tags at the end of the document.

    Args:
        markdown_text: Converted Markdown content.
        images_by_page: Dict mapping 1-indexed page numbers to image Path lists.

    Returns:
        Markdown string with embedded bundle-relative image references (images/...).
    """
    if not images_by_page:
        return markdown_text

    # Split by form-feed (page break)
    pages = markdown_text.split("\x0c")

    # Check if there is only 1 page but images have higher page numbers
    max_image_page = max(images_by_page.keys()) if images_by_page else 0
    if len(pages) == 1 and max_image_page > 1:
        logger.warning(
            "Markdown has only 1 page, but images found on higher pages. Appending all images at the end."
        )
        image_tags = []
        for page_num in sorted(images_by_page.keys()):
            for img_path in images_by_page[page_num]:
                rel_url = f"images/{img_path.name}"
                image_tags.append(f"\n\n![Page {page_num} Image]({rel_url})")
        return markdown_text + "".join(image_tags)

    new_pages = []

    for idx, page_content in enumerate(pages):
        page_num = idx + 1
        new_content = page_content

        # If we have images for this page, append them
        if page_num in images_by_page:
            image_tags = []
            for img_path in images_by_page[page_num]:
                rel_url = f"images/{img_path.name}"
                image_tags.append(f"\n\n![Page {page_num} Image]({rel_url})")
            new_content += "".join(image_tags)

        new_pages.append(new_content)

    return "\x0c".join(new_pages)


def _inject_docling_images(
    markdown_text: str,
    doc: Any,
    saved_images: list[tuple[int, Path]],
    canonical_dir: Path | None = None,
    alt_map: dict[int, str] | None = None,
) -> str:
    """Replace <!-- image --> placeholders in Docling markdown with canonical <figure data-type="image"> blocks.

    Wraps images and their captions in atomic HTML5 <figure> containers with concise
    alt-text, preventing caption duplication and keeping image/caption pairs intact.

    Args:
        markdown_text: Converted Markdown content exported by Docling.
        doc: Docling document AST object containing picture metadata.
        saved_images: List of tuples (picture_index, absolute_path) for saved PNGs.
        canonical_dir: Optional canonical directory for API compatibility.
        alt_map: Optional mapping of picture index to fallback alt/caption string.

    Returns:
        Markdown string with embedded <figure data-type="image"> references pointing to images/....
    """
    if not saved_images:
        return markdown_text

    try:
        from docling_core.types.doc import PictureItem
    except Exception:
        PictureItem = None

    body_pictures: list[tuple[int, Any]] = []
    if hasattr(doc, "iterate_items"):
        try:
            for item, _level in doc.iterate_items():
                is_pic = (
                    (PictureItem is not None and isinstance(item, PictureItem))
                    or getattr(item, "label", None) == "picture"
                    or (hasattr(item, "self_ref") and "#/pictures/" in str(item.self_ref))
                )
                if is_pic and hasattr(item, "self_ref"):
                    ref_str = str(item.self_ref)
                    try:
                        p_idx = int(ref_str.split("/")[-1])
                        body_pictures.append((p_idx, item))
                    except (ValueError, IndexError):
                        pass
        except Exception as e:
            logger.debug("Could not iterate body items from doc: %s", e)

    saved_map = {pic_idx: img_path for pic_idx, img_path in saved_images}

    def _format_figure_tag(
        pic_idx: int, img_path: Path, preceding_text: str = ""
    ) -> tuple[str, str]:
        """Format an extracted picture as a semantic HTML figure tag with captions.

        Args:
            pic_idx: Zero-based picture index.
            img_path: Path to the saved image file on disk.
            preceding_text: Text preceding the figure block in markdown.

        Returns:
            Tuple of (updated_preceding_text, formatted_figure_html).
        """
        caption = ""
        page_no = None
        if hasattr(doc, "pictures") and pic_idx < len(doc.pictures):
            pic = doc.pictures[pic_idx]
            if hasattr(pic, "caption_text"):
                try:
                    res_cap = pic.caption_text(doc)
                    if isinstance(res_cap, str):
                        caption = res_cap.strip()
                except Exception:
                    caption = ""
            if hasattr(pic, "prov") and pic.prov:
                try:
                    p = getattr(pic.prov[0], "page_no", None)
                    if isinstance(p, (int, str)):
                        page_no = p
                except Exception:
                    pass

        if not caption and alt_map and pic_idx in alt_map:
            caption = alt_map[pic_idx]

        alt_text = (
            f"Figure {pic_idx + 1}"
            if page_no is None
            else f"Figure {pic_idx + 1} (Page {page_no})"
        )
        img_link = f"![{alt_text}](images/{img_path.name})"

        mod_preceding = preceding_text
        caption_body = ""
        if caption:
            stripped_preceding = preceding_text.rstrip()
            if stripped_preceding.endswith(caption):
                cut_pos = stripped_preceding.rfind(caption)
                mod_preceding = stripped_preceding[:cut_pos]
                caption_body = caption
            else:
                caption_body = caption

        if caption_body:
            caption_line = (
                caption_body
                if caption_body.lower().startswith("fig")
                else f"**Figure {pic_idx + 1}.** {caption_body}"
            )
            fig_content = f"\n\n{img_link}\n\n{caption_line}\n"
        else:
            fig_content = f"\n\n{img_link}\n"

        fig_block = (
            f'<figure data-type="image" data-id="figure-{pic_idx:03d}">{fig_content}</figure>'
        )
        return mod_preceding, fig_block

    placeholder = "<!-- image -->"
    if placeholder not in markdown_text:
        figures = []
        for idx, p in saved_images:
            _, fig_tag = _format_figure_tag(idx, p)
            figures.append("\n\n" + fig_tag)
        return markdown_text + "".join(figures)

    parts = markdown_text.split(placeholder)
    result = []
    used_pic_indices = set()

    for idx, part in enumerate(parts):
        if idx < len(parts) - 1:
            pic_idx = None
            img_path = None
            if body_pictures and idx < len(body_pictures):
                b_idx, pic = body_pictures[idx]
                if b_idx in saved_map:
                    pic_idx = b_idx
                    img_path = saved_map[b_idx]
            elif idx < len(saved_images):
                pic_idx, img_path = saved_images[idx]

            if pic_idx is not None and img_path is not None:
                mod_part, fig_tag = _format_figure_tag(
                    pic_idx, img_path, preceding_text=part
                )
                result.append(mod_part)
                result.append(fig_tag)
                used_pic_indices.add(pic_idx)
            else:
                result.append(part)
                result.append(placeholder)
        else:
            result.append(part)

    # Append leftover images if there are more saved images than placeholders
    if not body_pictures and len(saved_images) > len(parts) - 1:
        leftovers = []
        for pic_idx, p in saved_images[len(parts) - 1 :]:
            if pic_idx not in used_pic_indices:
                _, fig_tag = _format_figure_tag(pic_idx, p)
                leftovers.append("\n\n" + fig_tag)
        result.append("".join(leftovers))

    return "".join(result)


def _inject_docling_tables(
    markdown_text: str,
    doc: Any,
    saved_tables: list[tuple[int, Path | None, Path | None]],
) -> str:
    """Wrap Docling extracted tables in canonical <figure data-type="table"> blocks.

    Args:
        markdown_text: Converted Markdown text.
        doc: Docling document AST object.
        saved_tables: List of tuples (table_index, csv_path, html_path).

    Returns:
        Markdown with tables enclosed in <figure data-type="table"> containers.
    """
    if not saved_tables and not getattr(doc, "tables", []):
        return wrap_standalone_markdown_tables(markdown_text)

    tables = getattr(doc, "tables", [])
    saved_map = {idx: (csv_p, html_p) for idx, csv_p, html_p in saved_tables}

    for idx, table in enumerate(tables):
        csv_p, html_p = saved_map.get(idx, (None, None))
        csv_rel = f"tables/table_{idx:03d}.csv" if csv_p and csv_p.exists() else None
        html_rel = f"tables/table_{idx:03d}.html" if html_p and html_p.exists() else None

        attr_parts = [f'data-type="table"', f'data-id="table-{idx:03d}"']
        artifact_refs = []
        if csv_rel:
            attr_parts.append(f'data-csv="{csv_rel}"')
            artifact_refs.append(csv_rel)
        if html_rel:
            attr_parts.append(f'data-html="{html_rel}"')
            artifact_refs.append(html_rel)

        attr_str = " ".join(attr_parts)
        artifact_comment = (
            f"\n<!-- artifacts: {' | '.join(artifact_refs)} -->"
            if artifact_refs
            else ""
        )

        caption = ""
        if hasattr(table, "caption_text"):
            try:
                caption = (table.caption_text(doc) or "").strip()
            except Exception:
                caption = ""

        table_md = ""
        if hasattr(table, "export_to_markdown"):
            try:
                res = table.export_to_markdown(doc=doc)
                if isinstance(res, str):
                    table_md = res.strip()
            except Exception:
                table_md = ""

        caption_header = (
            f"**Table {idx + 1}.** {caption}\n\n"
            if caption and not caption.lower().startswith("table")
            else (f"{caption}\n\n" if caption else "")
        )

        if table_md and table_md in markdown_text:
            search_str = table_md
            if caption:
                cap_idx = markdown_text.find(caption)
                t_idx = markdown_text.find(table_md)
                if 0 <= cap_idx < t_idx and (t_idx - (cap_idx + len(caption))) < 20:
                    search_str = markdown_text[cap_idx : t_idx + len(table_md)]
                    caption_header = f"{caption}\n\n"

            wrapped_block = (
                f'<figure {attr_str}>\n\n'
                f'{caption_header}'
                f'{table_md}'
                f'{artifact_comment}\n'
                f'</figure>'
            )
            markdown_text = markdown_text.replace(search_str, wrapped_block, 1)

    return wrap_standalone_markdown_tables(markdown_text)


def wrap_standalone_markdown_tables(markdown_text: str) -> str:
    """Wrap standalone GFM Markdown tables not already enclosed in <figure> tags.

    Args:
        markdown_text: Raw or converted Markdown text.

    Returns:
        Markdown with standalone tables enclosed in <figure data-type="table"> containers.
    """
    if not markdown_text:
        return markdown_text

    table_pattern = re.compile(
        r"(?:^|\n)((?:\|[^\n]+\|\r?\n)\|[\s\-:|]+\|(?:\r?\n\|[^\n]+\|)+)",
        re.MULTILINE,
    )

    def _replacer(match: re.Match) -> str:
        """Wrap an unencapsulated markdown table in semantic figure tags.

        Args:
            match: Regex match object containing the detected markdown table.

        Returns:
            Wrapped table string or original match if already inside a figure block.
        """
        matched_table = match.group(1).strip()
        start_pos = match.start()
        prefix = markdown_text[:start_pos]
        last_open = prefix.rfind("<figure")
        last_close = prefix.rfind("</figure>")
        if last_open > last_close:
            return match.group(0)

        return f'\n\n<figure data-type="table">\n\n{matched_table}\n\n</figure>\n\n'

    return table_pattern.sub(_replacer, markdown_text)


class BaseDocumentConverter(ABC):
    """Abstract base class for document conversion engines."""

    @property
    @abstractmethod
    def converter_id(self) -> str:
        """Unique identifier for the converter strategy (e.g., 'markitdown', 'direct_read')."""

    @property
    def converter_version(self) -> str:
        """Version string for cache invalidation. Override in subclasses when needed."""
        return "1.0.0"

    @property
    def is_remote(self) -> bool:
        """Indicate whether this converter relies on an external/remote network service."""
        return False

    @abstractmethod
    def supports(self, content_type: str, source_path: Path) -> bool:
        """Return True if this converter can process the given document.

        Args:
            content_type: MIME type of the document (e.g., 'application/pdf').
            source_path: Path to the source file on disk.
        """

    @abstractmethod
    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Convert source document to Markdown text.

        Args:
            item_key: Zotero item key used for identification.
            source_path: Path to the source file on disk.
            content_type: MIME type of the source file.
            cache_dir: Item-level bundle directory (``corpus/<item_key>/``).
                Converters should write extracted assets (images, tables) into
                subdirectories relative to this path (e.g., ``cache_dir / "images/"``).

        Returns:
            Converted Markdown text, or None if conversion failed or produced empty content.
        """


class DirectReadConverter(BaseDocumentConverter):
    """Direct UTF-8 reader for Markdown and plain text files (<1ms bypass)."""

    @property
    def converter_id(self) -> str:
        """Unique identifier for direct UTF-8 file reading strategy."""
        return "direct_read"

    def supports(self, content_type: str, source_path: Path) -> bool:
        """Check if content type or file extension corresponds to plain text or Markdown.

        Args:
            content_type: MIME type string.
            source_path: File path on disk.

        Returns:
            True if file is plain text or Markdown, False otherwise.
        """
        return (
            content_type in ("text/markdown", "text/plain")
            or source_path.suffix.lower() in (".md", ".txt", ".markdown")
        )

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Read Markdown or text content directly from disk.

        Args:
            item_key: Zotero item key.
            source_path: Path to the source file.
            content_type: MIME type of the file.
            cache_dir: Item-level bundle directory (unused by this converter).

        Returns:
            UTF-8 content string, or None if empty.
        """
        text = source_path.read_text(encoding="utf-8", errors="replace")
        if not text or not text.strip():
            logger.warning("Empty Markdown/Text file for %s", source_path)
            return None
        return text


class MarkItDownConverter(BaseDocumentConverter):
    """Document converter using Microsoft MarkItDown and pdfimages for embedded images."""

    @property
    def converter_id(self) -> str:
        """Unique identifier for Microsoft MarkItDown converter strategy."""
        return "markitdown"

    @property
    def converter_version(self) -> str:
        """Return the installed MarkItDown package version."""
        return _get_package_version("markitdown", fallback="0.1.5")

    def __init__(self, markitdown_instance: Any | None = None) -> None:
        """Initialize the MarkItDownConverter strategy.

        Args:
            markitdown_instance: Optional pre-configured MarkItDown instance.
        """
        self._md = markitdown_instance

    def supports(self, content_type: str, source_path: Path) -> bool:
        """MarkItDown serves as the default fallback converter for all supported documents.

        Args:
            content_type: MIME type of the document.
            source_path: Path to the source file on disk.

        Returns:
            Always returns True.
        """
        return True

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Convert document using MarkItDown and inject extracted PDF images if applicable.

        Args:
            item_key: Zotero item key.
            source_path: Path to the source file on disk.
            content_type: MIME type of the file.
            cache_dir: Item-level bundle directory (``corpus/<item_key>/``).

        Returns:
            Converted Markdown string, or None if conversion failed or returned empty text.
        """
        try:
            if self._md is None:
                markitdown_cls = getattr(sys.modules[__name__], "MarkItDown")
                self._md = markitdown_cls()
            result = self._md.convert(str(source_path))
        except (ImportError, Exception) as exc:
            if _is_vc_redist_dll_failure(exc):
                logger.error(
                    "Missing Microsoft Visual C++ Redistributable dependency during MarkItDown conversion: %s",
                    exc,
                )
                raise RuntimeError(VC_REDIST_WINDOWS_MSG) from exc
            raise

        text = result.text_content
        if not text or not text.strip():
            logger.warning("MarkItDown returned empty content for %s", source_path)
            return None

        # For PDFs, extract and inject images
        if content_type == "application/pdf":
            images_dir = cache_dir / "images"
            if images_dir.exists():
                shutil.rmtree(images_dir)

            images_by_page = _extract_pdf_images(source_path, images_dir)
            text = _embed_images_in_markdown(text, images_by_page)

        # For HTML documents, extract embedded data URIs / local images and wrap in <figure> blocks
        is_html = (
            content_type in ("text/html", "application/xhtml+xml")
            or source_path.suffix.lower() in (".html", ".htm", ".xhtml")
        )
        if is_html:
            images_dir = cache_dir / "images"
            if images_dir.exists():
                shutil.rmtree(images_dir)

            html_images = _extract_html_images(source_path, images_dir)
            canonical_dir = (
                cache_dir.parent / item_key
                if cache_dir.name.startswith(".tmp_")
                else cache_dir
            )
            text = _inject_html_images_into_markdown(
                text, html_images, canonical_dir=canonical_dir
            )

        return text


class DoclingLocalConverter(BaseDocumentConverter):
    """Document converter using IBM Docling for deep layout analysis and table extraction."""

    @property
    def converter_id(self) -> str:
        """Unique identifier for local IBM Docling converter strategy."""
        return "docling_local"

    @property
    def converter_version(self) -> str:
        """Return the installed docling package version with mode indicator."""
        base_v = _get_package_version(
            "docling",
            fallback=_get_package_version("docling-core", fallback="2.0.0"),
        )
        try:
            from zocrates.config import get_docling_mode, get_force_cpu_pref
            from zocrates.rag.hardware import is_high_performance_gpu

            mode = get_docling_mode()
            formula_active = (mode == "full") or (
                mode == "auto"
                and is_high_performance_gpu()
                and not get_force_cpu_pref()
            )
            if not formula_active:
                return f"{base_v}-fast"
        except Exception:
            pass
        return base_v

    def __init__(self, converter_instance: Any | None = None) -> None:
        """Initialize the DoclingLocalConverter strategy.

        Args:
            converter_instance: Optional pre-configured Docling DocumentConverter instance.
        """
        self._converter = converter_instance
        self._cpu_converter = None

    def supports(self, content_type: str, source_path: Path) -> bool:
        """Check if content type or file extension is supported by Docling.

        Supports PDF, HTML, DOCX, PPTX, XLSX, etc.
        """
        suffix = source_path.suffix.lower()
        supported_types = {
            "application/pdf",
            "text/html",
            "application/xhtml+xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        supported_extensions = {
            ".pdf",
            ".html",
            ".htm",
            ".xhtml",
            ".docx",
            ".pptx",
            ".xlsx",
            ".asciidoc",
        }
        return content_type in supported_types or suffix in supported_extensions

    def _create_converter_instance(self, force_cpu: bool = False) -> Any:
        """Instantiate a Docling DocumentConverter configured for CPU or hardware-accelerated execution."""
        from zocrates.config import get_docling_mode, get_force_cpu_pref
        from zocrates.rag.hardware import is_high_performance_gpu

        # Enable use_fast=True by default on AutoImageProcessor
        try:
            from transformers import AutoImageProcessor

            orig_from_pretrained = getattr(
                AutoImageProcessor, "_orig_from_pretrained", None
            )
            if orig_from_pretrained is None:
                orig_from_pretrained = AutoImageProcessor.from_pretrained
                AutoImageProcessor._orig_from_pretrained = orig_from_pretrained

                def _fast_from_pretrained(*args: Any, **kwargs: Any) -> Any:
                    """Invoke from_pretrained with use_fast=True default.

                    Args:
                        *args: Positional arguments forwarded to from_pretrained.
                        **kwargs: Keyword arguments forwarded to from_pretrained.

                    Returns:
                        Loaded processor instance.
                    """
                    kwargs.setdefault("use_fast", True)
                    return orig_from_pretrained(*args, **kwargs)

                AutoImageProcessor.from_pretrained = _fast_from_pretrained
        except Exception as e:
            logger.debug(
                "Could not configure use_fast default on AutoImageProcessor: %s", e
            )

        try:
            from docling.datamodel.pipeline_options import (
                AcceleratorDevice,
                AcceleratorOptions,
                ThreadedPdfPipelineOptions,
            )
            from docling.document_converter import (
                DocumentConverter,
                HTMLFormatOption,
                InputFormat,
                PdfFormatOption,
                PowerpointFormatOption,
                WordFormatOption,
            )

            docling_mode = get_docling_mode()
            use_cpu = force_cpu or get_force_cpu_pref()
            if not use_cpu:
                try:
                    import torch

                    if not torch.cuda.is_available():
                        use_cpu = True
                    else:
                        total_vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                        if total_vram_gb < 4.0:
                            logger.info(
                                "GPU VRAM (%.2f GB) is below 4.0 GB threshold; configuring Docling for CPU",
                                total_vram_gb,
                            )
                            use_cpu = True
                except Exception as e:
                    logger.debug("Could not verify CUDA VRAM capacity: %s; defaulting to CPU", e)
                    use_cpu = True

            acc_device = AcceleratorDevice.CPU if use_cpu else AcceleratorDevice.AUTO
            num_threads = min(4, max(1, (os.cpu_count() or 4) // 2))
            acc_options = AcceleratorOptions(device=acc_device, num_threads=num_threads)

            # Determine formula enrichment based on mode and hardware
            if docling_mode == "fast":
                do_formula = False
            elif docling_mode == "full":
                do_formula = not use_cpu
            else:  # "auto"
                do_formula = is_high_performance_gpu() and not use_cpu

            pipeline_options = ThreadedPdfPipelineOptions(
                do_formula_enrichment=do_formula,
                generate_picture_images=True,
                generate_page_images=True,
                accelerator_options=acc_options,
            )

            return DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    InputFormat.HTML: HTMLFormatOption(),
                    InputFormat.DOCX: WordFormatOption(),
                    InputFormat.PPTX: PowerpointFormatOption(),
                }
            )
        except Exception as exc:
            logger.error("Failed to initialize Docling DocumentConverter: %s", exc)
            raise

    def _get_converter(self, force_cpu: bool = False) -> Any:
        """Lazy-initialize or return the Docling DocumentConverter instance."""
        if force_cpu:
            if self._cpu_converter is None:
                self._cpu_converter = self._create_converter_instance(force_cpu=True)
            return self._cpu_converter
        if self._converter is None:
            self._converter = self._create_converter_instance(force_cpu=False)
        return self._converter

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Convert document using Docling and save AST, images, and tables to bundle.

        Args:
            item_key: Zotero item key.
            source_path: Path to the source file on disk.
            content_type: MIME type of the file.
            cache_dir: Item-level bundle directory (``corpus/<item_key>/``).

        Returns:
            Converted Markdown string, or None if conversion failed or returned empty text.
        """
        doc = None
        try:
            conv = self._get_converter()
            conv_result = conv.convert(str(source_path))
            doc = conv_result.document
        except (ImportError, Exception) as exc:
            if _is_vc_redist_dll_failure(exc):
                logger.error(
                    "Missing Microsoft Visual C++ Redistributable dependency during Docling conversion: %s",
                    exc,
                )
                raise RuntimeError(VC_REDIST_WINDOWS_MSG) from exc
            raw_exc_str = str(exc)
            exc_str = raw_exc_str.lower()
            is_oom = "out of memory" in exc_str or "cuda" in exc_str or "alloc" in exc_str
            if is_oom:
                logger.warning(
                    "Docling failed with device/OOM error on %s (%s). Attempting CPU fallback...",
                    source_path.name,
                    exc,
                )
                import gc
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        if hasattr(torch.cuda, "ipc_collect"):
                            torch.cuda.ipc_collect()
                except Exception:
                    pass

                try:
                    cpu_conv = self._get_converter(force_cpu=True)
                    conv_result = cpu_conv.convert(str(source_path))
                    doc = conv_result.document
                except (ImportError, Exception) as cpu_exc:
                    if _is_vc_redist_dll_failure(cpu_exc):
                        logger.error(
                            "Missing Microsoft Visual C++ Redistributable dependency during Docling CPU fallback: %s",
                            cpu_exc,
                        )
                        raise RuntimeError(VC_REDIST_WINDOWS_MSG) from cpu_exc
                    logger.error("Docling CPU fallback failed for %s: %s", source_path, cpu_exc)
                    gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                    return None
            else:
                logger.error("Docling conversion failed for %s: %s", source_path, exc)
                return None

        if doc is None:
            logger.warning("Docling returned empty document for %s", source_path)
            return None

        # 1. Export Markdown
        try:
            markdown = doc.export_to_markdown()
        except Exception as exc:
            logger.error("Docling markdown export failed for %s: %s", source_path, exc)
            return None

        if not markdown or not markdown.strip():
            logger.warning("Docling produced empty markdown for %s", source_path)
            return None

        # 2. Save full Docling document JSON AST to item bundle
        try:
            json_path = cache_dir / "docling_document.json"
            if hasattr(doc, "save_as_json"):
                doc.save_as_json(json_path)
            elif hasattr(doc, "model_dump_json"):
                json_path.write_text(doc.model_dump_json(), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save docling_document.json for %s: %s", item_key, exc)

        # 3. Extract and save pictures
        saved_images: list[tuple[int, Path]] = []
        alt_map: dict[int, str] = {}
        try:
            pictures = getattr(doc, "pictures", [])
            images_dir = cache_dir / "images"
            if images_dir.exists():
                shutil.rmtree(images_dir)
            if pictures:
                images_dir.mkdir(parents=True, exist_ok=True)
                for idx, pic in enumerate(pictures):
                    try:
                        img = pic.get_image(doc) if hasattr(pic, "get_image") else None
                        if img is not None:
                            img_path = images_dir / f"picture_{idx:03d}.png"
                            img.save(img_path, format="PNG")
                            saved_images.append((idx, img_path.resolve()))
                    except Exception as p_exc:
                        logger.debug(
                            "Could not extract image %d for %s: %s", idx, item_key, p_exc
                        )

            # If doc.pictures yielded no images (standard for HTML in Docling),
            # check if the source is HTML and extract embedded/local HTML images:
            is_html = (
                content_type in ("text/html", "application/xhtml+xml")
                or source_path.suffix.lower() in (".html", ".htm", ".xhtml")
            )
            if not saved_images and is_html:
                html_images = _extract_html_images(source_path, images_dir)
                saved_images = [(idx, img_p) for idx, img_p, _ in html_images]
                alt_map = {idx: alt for idx, _, alt in html_images if alt}
        except Exception as exc:
            logger.warning("Failed extracting pictures for %s: %s", item_key, exc)

        # Inject extracted image links into markdown (replacing <!-- image --> placeholders)
        canonical_dir = (
            cache_dir.parent / item_key
            if cache_dir.name.startswith(".tmp_")
            else cache_dir
        )
        markdown = _inject_docling_images(
            markdown, doc, saved_images, canonical_dir=canonical_dir, alt_map=alt_map
        )

        # 4. Extract and save tables (CSV & HTML)
        saved_tables: list[tuple[int, Path | None, Path | None]] = []
        try:
            tables = getattr(doc, "tables", [])
            tables_dir = cache_dir / "tables"
            if tables_dir.exists():
                shutil.rmtree(tables_dir)
            if tables:
                tables_dir.mkdir(parents=True, exist_ok=True)
                for idx, table in enumerate(tables):
                    csv_path = None
                    html_path = None
                    try:
                        if hasattr(table, "export_to_dataframe"):
                            df = table.export_to_dataframe(doc=doc)
                            if df is not None and not df.empty:
                                csv_path = tables_dir / f"table_{idx:03d}.csv"
                                df.to_csv(csv_path, index=False)
                        if hasattr(table, "export_to_html"):
                            html_content = table.export_to_html(doc=doc)
                            if html_content:
                                html_path = tables_dir / f"table_{idx:03d}.html"
                                html_path.write_text(html_content, encoding="utf-8")
                        saved_tables.append((idx, csv_path, html_path))
                    except Exception as t_exc:
                        logger.debug(
                            "Could not extract table %d for %s: %s", idx, item_key, t_exc
                        )
        except Exception as exc:
            logger.warning("Failed extracting tables for %s: %s", item_key, exc)

        # Wrap tables in <figure data-type="table"> blocks
        markdown = _inject_docling_tables(markdown, doc, saved_tables)

        # Release AST references and collect garbage to prevent memory retention
        del doc
        del conv_result
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

        return markdown


def _is_pdf_file(source_path: Path) -> bool:
    """Check if file starts with PDF magic bytes (%PDF-).

    Args:
        source_path: Path to file on disk.

    Returns:
        True if file exists and starts with %PDF-, False otherwise.
    """
    try:
        with open(source_path, "rb") as f:
            header = f.read(5)
            return header.startswith(b"%PDF-")
    except Exception:
        return False


def _html_to_markdown_inline(soup_or_tag: Any) -> str:
    """Recursively convert Datalab HTML DOM elements to clean Markdown with LaTeX math."""
    if soup_or_tag is None:
        return ""
    if isinstance(soup_or_tag, NavigableString):
        return str(soup_or_tag)

    tag = soup_or_tag
    name = getattr(tag, "name", None)
    if not name:
        return str(soup_or_tag)

    if name == "math":
        is_block = tag.get("display") == "block"
        raw_tex = tag.get_text().strip()
        if is_block:
            return f"\n\n$${raw_tex}$$\n\n"
        else:
            return f" ${raw_tex}$ "

    if name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        level = int(name[1])
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children).strip()
        return f"\n\n{'#' * level} {inner}\n\n"

    if name == "p":
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children).strip()
        return f"\n\n{inner}\n\n"

    if name in ["b", "strong"]:
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"**{inner}**"

    if name in ["i", "em"]:
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"*{inner}*"

    if name == "sup":
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"<sup>{inner}</sup>"

    if name == "sub":
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"<sub>{inner}</sub>"

    if name == "code":
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"`{inner}`"

    if name == "pre":
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"\n\n```\n{inner}\n```\n\n"

    if name == "a":
        href = tag.get("href", "")
        inner = "".join(_html_to_markdown_inline(c) for c in tag.children)
        return f"[{inner}]({href})" if href else inner

    if name == "img":
        src = tag.get("src", "")
        clean_src = f"images/{Path(src).name}" if src else ""
        alt = tag.get("alt", "")
        return f"![{alt}]({clean_src})" if clean_src else ""

    if name in ["ul", "ol"]:
        items = []
        is_ordered = (name == "ol")
        for idx, li in enumerate(tag.find_all("li", recursive=False), 1):
            li_inner = "".join(_html_to_markdown_inline(c) for c in li.children).strip()

            # Preserve explicit index if present (e.g., "1. ", "7. ", "1) ")
            num_match = re.match(r"^(\d+)[\.\)]\s+", li_inner)
            if num_match:
                item_num = num_match.group(1)
                clean_inner = re.sub(r"^\d+[\.\)]\s+", "", li_inner).strip()
                items.append(f"{item_num}. {clean_inner}")
            else:
                # Strip leading bullet glyphs (•, etc.) or standalone hyphen/asterisk (not **bold**)
                clean_inner = re.sub(r"^[•\u2022\u2023\u25E6\u2043\u2219]\s*", "", li_inner)
                clean_inner = re.sub(r"^[-*]\s+", "", clean_inner).strip()
                prefix = f"{idx}. " if is_ordered else "- "
                items.append(f"{prefix}{clean_inner}")
        return "\n\n" + "\n".join(items) + "\n\n"

    if name == "table":
        rows = []
        for tr in tag.find_all("tr"):
            cells = []
            for cell in tr.find_all(["th", "td"]):
                c_inner = "".join(_html_to_markdown_inline(c) for c in cell.children).strip()
                c_inner = re.sub(r"\s+", " ", c_inner)
                cells.append(c_inner)
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        max_cols = max(len(r) for r in rows)
        rows = [r + [""] * (max_cols - len(r)) for r in rows]
        header = "| " + " | ".join(rows[0]) + " |"
        sep = "| " + " | ".join(["---"] * max_cols) + " |"
        body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n\n" + "\n".join([header, sep] + body) + "\n\n"

    return "".join(_html_to_markdown_inline(c) for c in tag.children)


def generate_markdown_from_datalab_ast(ast_data: dict[str, Any]) -> str:
    """Build a cohesive Markdown document from Datalab AST with semantic <figure> containers.

    Walks the Datalab DOM tree (ast_data["children"]), pairs Figure/Diagram and Table blocks
    with their nearest Caption blocks on the same page, formats LaTeX formulas ($...$ / $$...$$),
    extracts synthetic descriptions cleanly without duplicate alt-text, and produces standard Markdown.

    Args:
        ast_data: Datalab JSON DOM tree containing pages and block children.

    Returns:
        Structured Markdown string.
    """
    pages = ast_data.get("children", [])
    doc_sections: list[str] = []

    for page_idx, page in enumerate(pages):
        children = page.get("children", [])
        if not children:
            continue

        # Step 1: Pair Figures/Diagrams and Tables with Captions on this page
        consumed_captions: set[str] = set()
        matched_pairs: dict[str, dict[str, Any]] = {}

        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for i, b in enumerate(children):
            btype = b.get("block_type")
            if btype in ["Figure", "Diagram", "Table", "Caption"]:
                candidates.append((i, btype, b))

        # Pass 1: Match Figures/Diagrams and Tables with Captions
        for idx, btype, b in candidates:
            bid = b.get("id", "")
            if btype in ["Figure", "Diagram"]:
                best_cap = None
                for c_idx, c_type, c_b in candidates:
                    c_id = c_b.get("id", "")
                    if c_type == "Caption" and c_id not in consumed_captions:
                        c_soup = BeautifulSoup(c_b.get("html", ""), "html.parser")
                        c_text = c_soup.get_text(separator=" ", strip=True)
                        if re.search(r"^(?:Fig|Figure|\*\*Fig|\*\*Figure)", c_text, re.IGNORECASE):
                            best_cap = c_b
                            if abs(c_idx - idx) == 1:
                                break
                if best_cap:
                    consumed_captions.add(best_cap.get("id", ""))
                    matched_pairs[bid] = best_cap

            elif btype == "Table":
                best_cap = None
                for c_idx, c_type, c_b in candidates:
                    c_id = c_b.get("id", "")
                    if c_type == "Caption" and c_id not in consumed_captions:
                        c_soup = BeautifulSoup(c_b.get("html", ""), "html.parser")
                        c_text = c_soup.get_text(separator=" ", strip=True)
                        if re.search(r"^(?:Table|\*\*Table)", c_text, re.IGNORECASE):
                            best_cap = c_b
                            if abs(c_idx - idx) == 1:
                                break
                if best_cap:
                    consumed_captions.add(best_cap.get("id", ""))
                    matched_pairs[bid] = best_cap

        # Step 2: Render blocks in page order
        for b in children:
            btype = b.get("block_type")
            bid = b.get("id", "")

            # Skip page headers and footers
            if btype in ["PageHeader", "PageFooter"]:
                continue

            # Skip captions that have been bundled into a Figure or Table
            if btype == "Caption" and bid in consumed_captions:
                continue

            raw_html = b.get("html", "")
            soup = BeautifulSoup(raw_html, "html.parser")

            if btype in ["Figure", "Diagram"]:
                img_tag = soup.find("img")
                img_src = img_tag.get("src", "") if img_tag else ""
                clean_src = f"images/{Path(img_src).name}" if img_src else ""

                synthetic_caption = ""
                img_desc = soup.find("div", class_="img-description")
                if img_desc:
                    alt_div = img_desc.find("div", class_="img-alt")
                    if alt_div and len(img_desc.find_all(recursive=False)) > 1:
                        alt_div.decompose()
                    synthetic_caption = _html_to_markdown_inline(img_desc).strip()
                elif img_tag and img_tag.get("alt"):
                    synthetic_caption = img_tag["alt"].strip()

                # Get matched author caption
                matched_cap_block = matched_pairs.get(bid)
                author_caption = ""
                if matched_cap_block:
                    cap_soup = BeautifulSoup(matched_cap_block.get("html", ""), "html.parser")
                    author_caption = _html_to_markdown_inline(cap_soup).strip()

                # Clean alt text
                clean_alt = "Figure"
                if author_caption:
                    alt_m = re.search(r'\*\*(Fig(?:ure|\.)?\s+[^\n*]+)\*\*', author_caption, re.IGNORECASE)
                    if alt_m:
                        clean_alt = alt_m.group(1).rstrip(".: ")

                fig_parts = ['<figure data-type="image">\n']
                if clean_src:
                    fig_parts.append(f'![{clean_alt}]({clean_src})\n')
                if synthetic_caption:
                    fig_parts.append(f'<synthetic_caption data-source="datalab">\n{synthetic_caption}\n</synthetic_caption>\n')
                if author_caption:
                    fig_parts.append(f'{author_caption}\n')
                fig_parts.append('</figure>')

                doc_sections.append("\n\n" + "\n".join(fig_parts) + "\n\n")

            elif btype == "Table":
                table_md = _html_to_markdown_inline(soup).strip()
                matched_cap_block = matched_pairs.get(bid)
                author_caption = ""
                if matched_cap_block:
                    cap_soup = BeautifulSoup(matched_cap_block.get("html", ""), "html.parser")
                    author_caption = _html_to_markdown_inline(cap_soup).strip()

                fig_parts = ['<figure data-type="table">\n']
                if author_caption:
                    fig_parts.append(f'{author_caption}\n')
                fig_parts.append(f'{table_md}\n')
                fig_parts.append('</figure>')

                doc_sections.append("\n\n" + "\n".join(fig_parts) + "\n\n")

            elif btype == "Equation":
                math_tag = soup.find("math")
                if math_tag:
                    raw_tex = math_tag.get_text().strip()
                    doc_sections.append(f"\n\n$${raw_tex}$$\n\n")
                else:
                    doc_sections.append(_html_to_markdown_inline(soup))

            else:
                block_md = _html_to_markdown_inline(soup).strip()
                if block_md:
                    doc_sections.append(f"\n\n{block_md}\n\n")

    full_md = "".join(doc_sections)
    full_md = re.sub(r'\n{3,}', '\n\n', full_md)
    return full_md.strip() + "\n"


def _format_datalab_markdown(markdown_text: str) -> str:
    """Transform raw Datalab Markdown into cleanly delimited <figure> containers.

    Fallback used when Datalab JSON AST is absent.

    Args:
        markdown_text: Raw Markdown emitted by Datalab.

    Returns:
        Processed Markdown with <figure data-type="image"> and <figure data-type="table">.
    """
    if not markdown_text:
        return markdown_text

    text = markdown_text.replace("\r\n", "\n")

    # If already wrapped in figures, avoid re-wrapping
    if '<figure data-type="image">' in text and '<figure data-type="table">' in text:
        return text

    # Step 1: Match images and associate captions / synthetic descriptions
    img_block_regex = re.compile(
        r'(?:((?:^|\n\n)\*\*(?:Fig(?:ure|\.)?|FIG(?:URE|\.)?)\s+[^\n*]+\*\*[^\n]*?(?:\n[^\n#|!]+)*\n*))?'
        r'!\[([\s\S]*?)\]\(([^\)\s]+\.(?:jpg|jpeg|png|webp|svg|gif))\)'
        r'(?:\s*([\s\S]*?)(?=(?:<!-- BEGIN IMAGE CAPTION -->|<synthetic_caption>|\n\n\*\*Fig|\n\n<|\n\n#|\Z)))?'
        r'(?:\s*(?:<!-- BEGIN IMAGE CAPTION -->([\s\S]*?)<!-- END IMAGE CAPTION -->|<synthetic_caption>([\s\S]*?)<\/synthetic_caption>))?'
        r'(?:(\n+\*\*(?:Fig(?:ure|\.)?|FIG(?:URE|\.)?)\s+[^\n*]+\*\*[^\n]*?(?:\n[^\n#|!]+)*(?:\n\n|\Z)))?',
        re.MULTILINE,
    )

    def _replace_figure(m: re.Match) -> str:
        """Format extracted markdown image with synthetic and author captions into figure tags.

        Args:
            m: Regex match object containing image components and captions.

        Returns:
            Semantic HTML figure block string for the image.
        """
        prec_caption = (m.group(1) or "").strip()
        raw_alt = (m.group(2) or "").strip()
        raw_src = (m.group(3) or "").strip()
        intermediate_desc = (m.group(4) or "").strip()
        synth_cap_1 = m.group(5)
        synth_cap_2 = m.group(6)
        succ_caption = (m.group(7) or "").strip()

        img_filename = Path(raw_src).name
        clean_src = f"images/{img_filename}"

        synthetic_caption = (synth_cap_1 or synth_cap_2 or "").strip()
        extra_desc = ""
        if intermediate_desc and intermediate_desc != synthetic_caption:
            extra_desc = intermediate_desc

        if not synthetic_caption and len(raw_alt) > 50:
            synthetic_caption = raw_alt

        author_caption = prec_caption or succ_caption or ""
        clean_alt = "Figure"
        if author_caption:
            alt_m = re.search(r'\*\*(Fig(?:ure|\.)?\s+[^\n*]+)\*\*', author_caption, re.IGNORECASE)
            if alt_m:
                clean_alt = alt_m.group(1).rstrip(".: ")
        elif raw_alt and len(raw_alt) <= 40:
            clean_alt = raw_alt

        body_parts = [
            '<figure data-type="image">\n',
            f'![{clean_alt}]({clean_src})\n',
        ]
        if synthetic_caption:
            body_parts.append(f'<synthetic_caption>\n{synthetic_caption}\n</synthetic_caption>\n')
        if extra_desc:
            body_parts.append(f'{extra_desc}\n')
        if author_caption:
            body_parts.append(f'{author_caption}\n')
        body_parts.append('</figure>')

        return "\n\n" + "\n".join(body_parts) + "\n\n"

    # Step 2: Match tables and associate preceding or succeeding captions
    table_block_regex = re.compile(
        r'(?:((?:^|\n\n)\*\*(?:Table|TABLE)\s+[^\n*]+\*\*[^\n]*?(?:\n[^\n#|]+)*\n+))?'
        r'(\|[^\n]+\|\n\|[\s:|-]+\|\n(?:\|[^\n]+\|\n*)+)'
        r'(?:(\n+\*\*(?:Table|TABLE)\s+[^\n*]+\*\*[^\n]*?(?:\n[^\n#|]+)*(?:\n\n|\Z)))?',
        re.MULTILINE,
    )

    def _replace_table(m: re.Match) -> str:
        """Format extracted markdown table with surrounding captions into figure tags.

        Args:
            m: Regex match object containing table and caption components.

        Returns:
            Semantic HTML figure block string for the table.
        """
        prec_caption = (m.group(1) or "").strip()
        table_md = m.group(2).strip()
        succ_caption = (m.group(3) or "").strip()

        if "<figure" in prec_caption or "</figure>" in prec_caption or "<figure" in table_md:
            return m.group(0)

        author_caption = prec_caption or succ_caption or ""
        body_parts = ['<figure data-type="table">\n']
        if prec_caption:
            body_parts.append(f'{prec_caption}\n')
        body_parts.append(f'{table_md}\n')
        if succ_caption and not prec_caption:
            body_parts.append(f'{succ_caption}\n')
        body_parts.append('</figure>')

        return "\n\n" + "\n".join(body_parts) + "\n\n"

    processed = img_block_regex.sub(_replace_figure, text)
    processed = table_block_regex.sub(_replace_table, processed)
    processed = wrap_standalone_markdown_tables(processed)
    processed = re.sub(r'\n{3,}', '\n\n', processed)
    return processed.strip() + "\n"


class DatalabConverter(BaseDocumentConverter):
    """Remote document conversion engine via Datalab OCR API."""

    def __init__(self) -> None:
        """Initialize the remote Datalab converter instance."""
        self._metadata: dict[str, dict[str, Any]] = {}

    @property
    def converter_id(self) -> str:
        """Unique identifier for remote Datalab OCR converter strategy."""
        return "datalab"

    @property
    def converter_version(self) -> str:
        """Version string of the installed Datalab SDK."""
        return _get_package_version("datalab-python-sdk", "1.0.0")

    @property
    def is_remote(self) -> bool:
        """Indicate whether conversion relies on a remote cloud service."""
        return True

    def supports(self, content_type: str, source_path: Path) -> bool:
        """Check if document format is a PDF supported by Datalab.

        Strictly limits conversion to PDF documents only.

        Args:
            content_type: MIME type string.
            source_path: File path on disk.

        Returns:
            True if file is a PDF, False otherwise.
        """
        return (
            content_type == "application/pdf"
            or source_path.suffix.lower() == ".pdf"
            or _is_pdf_file(source_path)
        )

    def get_metadata(self, item_key: str) -> dict[str, Any]:
        """Return the latest conversion metadata for an item key.

        Args:
            item_key: Zotero item key.

        Returns:
            Dictionary containing parse_quality_score, checkpoint_id, etc.
        """
        return self._metadata.get(item_key, {})

    def convert(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Convert document via Datalab API with adaptive retry and asset unpacking.

        Includes an extension safeguard for valid PDFs lacking a .pdf extension.

        Args:
            item_key: Zotero item key.
            source_path: Source file path on disk.
            content_type: MIME type string.
            cache_dir: Bundle directory (or temporary staging directory).

        Returns:
            Converted Markdown string with <figure> tags, or None on failure.
        """
        import tempfile

        if source_path.suffix.lower() != ".pdf":
            with tempfile.TemporaryDirectory() as tmpdir:
                staged_path = Path(tmpdir) / f"{source_path.stem}.pdf"
                shutil.copy2(source_path, staged_path)
                return self._convert_internal(item_key, staged_path, content_type, cache_dir)
        else:
            return self._convert_internal(item_key, source_path, content_type, cache_dir)

    def _convert_internal(
        self,
        item_key: str,
        source_path: Path,
        content_type: str,
        cache_dir: Path,
    ) -> str | None:
        """Internal conversion routine executing against Datalab API."""
        import base64
        import json
        from zocrates.config import get_datalab_api_key, get_datalab_merge_cross_page

        api_key = get_datalab_api_key()
        if not api_key:
            raise RuntimeError(
                "Datalab API key not configured. Set DATALAB_API_KEY environment variable "
                "or save your API key in Zocrates Preferences."
            )

        try:
            from datalab_sdk import ConvertOptions, DatalabClient
        except ImportError as exc:
            raise RuntimeError(
                "datalab-python-sdk package is not installed. Install with 'uv add datalab-python-sdk'."
            ) from exc

        merge_cross_page = get_datalab_merge_cross_page(getattr(self, "_zotero_data_dir", None))

        client = DatalabClient(api_key=api_key)

        options = ConvertOptions(
            output_format="markdown,json",
            mode="balanced",
            paginate=False,
            disable_image_extraction=False,
            disable_image_captions=False,
            fence_synthetic_captions=True,
            save_checkpoint=True,
            extras="new_block_types",
            merge_cross_page=merge_cross_page,
        )

        try:
            result = client.convert(source_path, options=options)
        except Exception as exc:
            logger.error("Datalab API request failed for %s: %s", source_path, exc)
            raise RuntimeError(f"Datalab API request failed: {exc}") from exc

        if not getattr(result, "success", False) or not getattr(result, "markdown", None):
            error_msg = getattr(result, "error", None) or "Datalab conversion returned unsuccessful status"
            raise RuntimeError(f"Datalab conversion failed: {error_msg}")

        # Adaptive Quality Guardrail: If parse quality is below 3.0 / 5.0, retry in 'accurate' mode
        def _extract_score(res: Any) -> float | None:
            """Extract parse quality score from Datalab response object.

            Args:
                res: Datalab conversion result or response object.

            Returns:
                Float quality score out of 5.0, or None if unavailable.
            """
            s = getattr(res, "parse_quality_score", None)
            if s is not None:
                return float(s)
            if isinstance(getattr(res, "json", None), dict):
                if res.json.get("parse_quality_score") is not None:
                    return float(res.json["parse_quality_score"])
                if isinstance(res.json.get("metadata"), dict) and res.json["metadata"].get("parse_quality_score") is not None:
                    return float(res.json["metadata"]["parse_quality_score"])
            if isinstance(getattr(res, "metadata", None), dict) and res.metadata.get("parse_quality_score") is not None:
                return float(res.metadata["parse_quality_score"])
            return None

        score = _extract_score(result)

        if score is not None and score < 3.0:
            logger.info(
                "Parse quality score %.2f/5.0 for %s is below threshold (3.0). "
                "Retrying conversion with mode='accurate'...",
                score,
                item_key,
            )
            options_accurate = ConvertOptions(
                output_format="markdown,json",
                mode="accurate",
                paginate=False,
                disable_image_extraction=False,
                disable_image_captions=False,
                fence_synthetic_captions=True,
                save_checkpoint=True,
                extras="new_block_types",
                merge_cross_page=merge_cross_page,
            )
            try:
                retry_result = client.convert(source_path, options=options_accurate)
                if getattr(retry_result, "success", False) and getattr(retry_result, "markdown", None):
                    result = retry_result
                    retry_score = _extract_score(result)
                    if retry_score is not None:
                        score = retry_score
            except Exception as retry_exc:
                logger.warning(
                    "Datalab 'accurate' mode retry failed for %s: %s; keeping balanced result",
                    item_key,
                    retry_exc,
                )

        checkpoint_id = getattr(result, "checkpoint_id", None)
        self._metadata[item_key] = {
            "parse_quality_score": score,
            "checkpoint_id": checkpoint_id,
        }

        # 1. Unpack images
        images_dict = getattr(result, "images", None)
        if images_dict:
            images_dir = cache_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            resolved_images_dir = images_dir.resolve()
            for filename, raw_data in images_dict.items():
                try:
                    safe_filename = Path(str(filename)).name
                    if not safe_filename or safe_filename in (".", "..") or safe_filename != str(filename):
                        logger.warning(
                            "Blocked path traversal or invalid image filename %r in Datalab response for %s",
                            filename,
                            item_key,
                        )
                        continue
                    img_path = (images_dir / safe_filename).resolve()
                    if not img_path.is_relative_to(resolved_images_dir):
                        logger.warning(
                            "Blocked path traversal attempt in Datalab image %r for %s",
                            filename,
                            item_key,
                        )
                        continue
                    if isinstance(raw_data, bytes):
                        img_path.write_bytes(raw_data)
                    elif isinstance(raw_data, str):
                        b64_str = raw_data.split(",", 1)[-1] if "," in raw_data else raw_data
                        img_path.write_bytes(base64.b64decode(b64_str))
                except Exception as img_exc:
                    logger.warning("Failed saving image %s for %s: %s", filename, item_key, img_exc)

        # 2. Save JSON AST / metadata to datalab_document.json
        try:
            json_path = cache_dir / "datalab_document.json"
            ast_data = getattr(result, "json", None)
            if ast_data and isinstance(ast_data, dict):
                ast_copy = dict(ast_data)
                ast_copy["parse_quality_score"] = score
                ast_copy["checkpoint_id"] = checkpoint_id
                json_path.write_text(json.dumps(ast_copy, indent=2), encoding="utf-8")
            else:
                meta_dump = {
                    "parse_quality_score": score,
                    "checkpoint_id": checkpoint_id,
                    "runtime": getattr(result, "runtime", None),
                    "page_count": getattr(result, "page_count", None),
                }
                json_path.write_text(json.dumps(meta_dump, indent=2), encoding="utf-8")
        except Exception as json_exc:
            logger.warning("Failed saving datalab_document.json for %s: %s", item_key, json_exc)

        # 3. Post-process markdown (<figure> wrapping for images, synthetic captions, tables)
        ast_data = getattr(result, "json", None)
        if isinstance(ast_data, dict) and ast_data.get("children"):
            markdown = generate_markdown_from_datalab_ast(ast_data)
        else:
            markdown = _format_datalab_markdown(result.markdown or "")
        return markdown


def __getattr__(name: str) -> Any:
    if name == "MarkItDown":
        try:
            from markitdown import MarkItDown

            globals()["MarkItDown"] = MarkItDown
            return MarkItDown
        except (ImportError, Exception) as e:
            if _is_vc_redist_dll_failure(e):
                logger.error(
                    "Missing Microsoft Visual C++ Redistributable dependency when importing MarkItDown: %s",
                    e,
                )
                raise RuntimeError(VC_REDIST_WINDOWS_MSG) from e
            raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

