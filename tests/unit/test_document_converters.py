"""Unit tests for document converters and image extraction/injection."""

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter

from zocrates.rag.document_converters import (
    _extract_pdf_images,
    _inject_html_images_into_markdown,
)


def test_inject_html_images_adjacent_in_link():
    """Verify that multiple adjacent images inside an <a> link are matched individually.

    Prevents capturing subsequent image syntax and data: URIs into alt text.
    """
    markdown_text = (
        "[![OpenRouter](data:image/png;base64,AAA)"
        "![OpenRouter](data:image/svg+xml;base64,BBB)](https://openrouter.ai/)"
    )
    html_images = [
        (0, Path("picture_000.png"), "OpenRouter"),
        (1, Path("picture_001.svg"), "OpenRouter"),
    ]

    result = _inject_html_images_into_markdown(markdown_text, html_images)

    # Both images should be wrapped in their own figure blocks
    assert '<figure data-type="image" data-id="figure-000">' in result
    assert '<figure data-type="image" data-id="figure-001">' in result
    assert "![OpenRouter](images/picture_000.png)" in result
    assert "![OpenRouter](images/picture_001.svg)" in result

    # Alt text and captions must not contain data: URIs or markdown link delimiters
    assert "data:image/" not in result
    assert "data:" not in result
    assert "](" not in result.split("![OpenRouter]")[1].split("</figure>")[0]
    assert "**Figure 1.** OpenRouter" in result
    assert "**Figure 2.** OpenRouter" in result


def test_inject_html_images_sanitizes_corrupted_alt():
    """Verify alt_str sanitization falls back to 'Figure {pic_idx + 1}' when containing data: or ](."""
    markdown_text = "![malicious](data:image/png;base64,XYZ)"
    # Corrupted alt text containing data URI and link delimiter
    html_images = [
        (0, Path("picture_000.png"), "bad](data:image/png;base64,XYZ"),
    ]

    result = _inject_html_images_into_markdown(markdown_text, html_images)

    assert '<figure data-type="image" data-id="figure-000">' in result
    assert "data:" not in result
    assert "](" not in result.split("![")[1].split("]")[0]
    # Should fall back cleanly to Figure 1
    assert "![malicious](images/picture_000.png)" in result or "![Figure 1](images/picture_000.png)" in result


def test_inject_html_images_single_and_standalone():
    """Verify single linked image and standalone image tags are correctly converted to <figure> tags."""
    markdown_text = (
        "Intro text\n\n"
        "[![Single Logo](data:image/png;base64,123)](https://example.com)\n\n"
        "Middle text\n\n"
        "![Standalone Chart](data:image/png;base64,456)\n\n"
        "End text"
    )
    html_images = [
        (0, Path("logo.png"), "Single Logo"),
        (1, Path("chart.png"), "Standalone Chart"),
    ]

    result = _inject_html_images_into_markdown(markdown_text, html_images)

    assert '<figure data-type="image" data-id="figure-000">' in result
    assert '<figure data-type="image" data-id="figure-001">' in result
    assert "![Single Logo](images/logo.png)" in result
    assert "![Standalone Chart](images/chart.png)" in result
    assert "data:image/" not in result


def test_extract_pdf_images_pypdf_fallback(tmp_path):
    """Verify _extract_pdf_images falls back to pypdf when pdfimages CLI is not installed."""
    # Create a 2-page PDF with 1 image on each page
    img = Image.new("RGB", (80, 80), color="blue")
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF")
    pdf_buf.seek(0)

    reader = PdfReader(pdf_buf)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.add_page(reader.pages[0])

    pdf_path = tmp_path / "test_document.pdf"
    with open(pdf_path, "wb") as f:
        writer.write(f)

    output_dir = tmp_path / "images"

    with patch("shutil.which", return_value=None):
        images_by_page = _extract_pdf_images(pdf_path, output_dir)

    # Should have extracted images for page 1 and page 2 (1-indexed)
    assert 1 in images_by_page
    assert 2 in images_by_page
    assert len(images_by_page[1]) == 1
    assert len(images_by_page[2]) == 1

    img_p1 = images_by_page[1][0]
    img_p2 = images_by_page[2][0]

    assert img_p1.name.startswith("img_p1_0")
    assert img_p2.name.startswith("img_p2_0")
    assert img_p1.exists()
    assert img_p2.exists()
    assert img_p1.stat().st_size > 0
    assert img_p2.stat().st_size > 0


def test_extract_pdf_images_pypdf_fallback_error_handling(tmp_path):
    """Verify _extract_pdf_images gracefully returns empty dict on unreadable or corrupted PDF."""
    corrupted_pdf = tmp_path / "corrupted.pdf"
    corrupted_pdf.write_bytes(b"Not a valid PDF stream")

    output_dir = tmp_path / "images"

    with patch("shutil.which", return_value=None):
        res = _extract_pdf_images(corrupted_pdf, output_dir)

    assert res == {}
