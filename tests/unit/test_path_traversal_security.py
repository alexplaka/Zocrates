"""
Unit tests for path traversal security and containment guards in document conversion.

Tests:
1. _extract_html_images strict containment within source_path.parent (blocks ../ and /etc/passwd).
2. Datalab image unpacking blocks traversal filenames (../../evil.png, /etc/passwd, empty, dots).
3. _resolve_attachment_path blocks storage:../../etc/passwd and relative traversals.
4. _resolve_image_paths / normalize_image_relative_path in vlm_captioner.py confines paths to bundle_dir.
5. Regression: legitimate local images, base64 images, and valid attachments resolve and convert correctly.
"""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.rag.document_converters import (
    DatalabConverter,
    _extract_html_images,
)
from zocrates.rag.vlm_captioner import (
    normalize_image_relative_path,
)
from zocrates.rag.zotero_db import ZoteroDB


# ---------------------------------------------------------------------------
# 1. HTML Image Extraction Path Containment Tests
# ---------------------------------------------------------------------------


class TestHtmlImageExtractionSecurity:
    """Verify _extract_html_images blocks traversal attempts while allowing safe images."""

    def test_extract_html_images_blocks_relative_traversal(self, tmp_path):
        """Relative paths escaping source_path.parent (e.g. ../secret.png) must be blocked."""
        # Setup: secret file outside HTML directory
        secret_dir = tmp_path / "secret_zone"
        secret_dir.mkdir()
        secret_file = secret_dir / "secret.png"
        secret_file.write_bytes(b"CONFIDENTIAL_IMAGE_DATA")

        html_dir = tmp_path / "html_zone"
        html_dir.mkdir()
        html_file = html_dir / "document.html"
        output_dir = tmp_path / "extracted"
        output_dir.mkdir()

        html_content = """
        <html>
          <body>
            <img src="../secret_zone/secret.png" alt="Stolen Secret" />
            <img src="../../etc/passwd" alt="Passwd" />
          </body>
        </html>
        """
        html_file.write_text(html_content, encoding="utf-8")

        extracted = _extract_html_images(html_file, output_dir)

        # Neither traversal image should be extracted
        assert len(extracted) == 0
        assert not (output_dir / "picture_000.png").exists()
        assert not (output_dir / "picture_001.png").exists()

    def test_extract_html_images_blocks_absolute_paths(self, tmp_path):
        """Absolute paths (e.g. /etc/passwd or tmp file) must be blocked."""
        ext_dir = tmp_path / "external"
        ext_dir.mkdir()
        ext_file = ext_dir / "external.png"
        ext_file.write_bytes(b"EXTERNAL_BYTES")

        html_dir = tmp_path / "html_zone"
        html_dir.mkdir()
        html_file = html_dir / "doc.html"
        output_dir = tmp_path / "extracted"
        output_dir.mkdir()

        html_content = f"""
        <html>
          <body>
            <img src="{ext_file.as_posix()}" alt="Absolute path traversal" />
            <img src="/etc/passwd" alt="Absolute /etc/passwd" />
          </body>
        </html>
        """
        html_file.write_text(html_content, encoding="utf-8")

        extracted = _extract_html_images(html_file, output_dir)

        assert len(extracted) == 0
        assert list(output_dir.iterdir()) == []

    def test_extract_html_images_skips_non_image_extensions(self, tmp_path):
        """Files without an allowlisted image extension (.txt, .pdf, .sh) must not be copied."""
        html_dir = tmp_path / "html_zone"
        html_dir.mkdir()
        secret_txt = html_dir / "secret.txt"
        secret_txt.write_text("API_SECRET_KEY=12345", encoding="utf-8")
        script_sh = html_dir / "script.sh"
        script_sh.write_text("echo hello", encoding="utf-8")

        output_dir = tmp_path / "extracted"
        output_dir.mkdir()

        html_file = html_dir / "doc.html"
        html_content = """
        <html>
          <body>
            <img src="secret.txt" alt="Secret Text" />
            <img src="script.sh" alt="Script" />
          </body>
        </html>
        """
        html_file.write_text(html_content, encoding="utf-8")

        extracted = _extract_html_images(html_file, output_dir)
        assert len(extracted) == 0
        assert list(output_dir.iterdir()) == []

    def test_extract_html_images_allows_legitimate_local_images(self, tmp_path):
        """Legitimate local images within source_path.parent (same dir or subdir) must succeed."""
        html_dir = tmp_path / "html_zone"
        html_dir.mkdir()
        sub_dir = html_dir / "sub"
        sub_dir.mkdir()

        img1 = html_dir / "fig1.png"
        img1.write_bytes(b"\x89PNG\r\n\x1a\nfig1_content")

        img2 = sub_dir / "fig2.jpg"
        img2.write_bytes(b"\xff\xd8\xfffig2_content")

        img3 = html_dir / "fig3.png"
        img3.write_bytes(b"\x89PNG\r\n\x1a\nfig3_content")

        output_dir = tmp_path / "extracted"
        output_dir.mkdir()

        html_file = html_dir / "doc.html"
        html_content = """
        <html>
          <body>
            <img src="fig1.png" alt="Figure 1" />
            <img src="sub/fig2.jpg" alt="Figure 2" />
            <img src="./fig3.png" alt="Figure 3" />
          </body>
        </html>
        """
        html_file.write_text(html_content, encoding="utf-8")

        extracted = _extract_html_images(html_file, output_dir)

        assert len(extracted) == 3
        # Indices and file paths
        assert extracted[0][0] == 0
        assert extracted[0][1].name == "picture_000.png"
        assert extracted[0][1].read_bytes() == b"\x89PNG\r\n\x1a\nfig1_content"
        assert extracted[0][2] == "Figure 1"

        assert extracted[1][0] == 1
        assert extracted[1][1].name == "picture_001.jpg"
        assert extracted[1][1].read_bytes() == b"\xff\xd8\xfffig2_content"

        assert extracted[2][0] == 2
        assert extracted[2][1].name == "picture_002.png"
        assert extracted[2][1].read_bytes() == b"\x89PNG\r\n\x1a\nfig3_content"

    def test_extract_html_images_allows_base64_data_uris(self, tmp_path):
        """Base64 data URIs should decode and extract normally."""
        html_dir = tmp_path / "html_zone"
        html_dir.mkdir()
        html_file = html_dir / "doc.html"
        output_dir = tmp_path / "extracted"
        output_dir.mkdir()

        raw_bytes = b"\x89PNG\r\n\x1a\nfake_png_data"
        b64 = base64.b64encode(raw_bytes).decode("ascii")

        html_content = f"""
        <html>
          <body>
            <img src="data:image/png;base64,{b64}" alt="Embedded base64" />
          </body>
        </html>
        """
        html_file.write_text(html_content, encoding="utf-8")

        extracted = _extract_html_images(html_file, output_dir)

        assert len(extracted) == 1
        assert extracted[0][1].name == "picture_000.png"
        assert extracted[0][1].read_bytes() == raw_bytes
        assert extracted[0][2] == "Embedded base64"


# ---------------------------------------------------------------------------
# 2. Datalab Converter Image Unpacking Security Tests
# ---------------------------------------------------------------------------


class TestDatalabConverterImageUnpackingSecurity:
    """Verify DatalabConverter._convert_internal image unpacking blocks traversal filenames."""

    def test_datalab_blocks_traversal_filenames(self, tmp_path):
        """Image keys containing traversal components (../../evil.png, /etc/passwd) must be skipped."""
        conv = DatalabConverter()

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        secret_target = tmp_path / "evil.png"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.images = {
            "../../evil.png": b"MALICIOUS_BYTES_1",
            "/etc/passwd": b"MALICIOUS_BYTES_2",
            "../sub/evil.png": b"MALICIOUS_BYTES_3",
            "..": b"MALICIOUS_BYTES_4",
            ".": b"MALICIOUS_BYTES_5",
            "": b"MALICIOUS_BYTES_6",
            "legitimate.png": b"VALID_IMAGE_BYTES",
        }
        mock_result.json = None
        mock_result.markdown = "# Sample Datalab Doc\n\n![Valid](images/legitimate.png)"
        mock_result.runtime = 1.0
        mock_result.page_count = 1
        mock_result.parse_quality_score = 4.5

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_result

        with (
            patch("zocrates.config.get_datalab_api_key", return_value="dummy-key"),
            patch("datalab_sdk.DatalabClient", return_value=mock_client),
        ):
            input_file = tmp_path / "test.pdf"
            input_file.write_bytes(b"%PDF-1.4 dummy")

            md = conv._convert_internal("TEST_KEY", input_file, "application/pdf", cache_dir)

        # None of the malicious files should be written outside or inside images_dir
        assert not secret_target.exists()
        images_dir = cache_dir / "images"
        assert images_dir.exists()

        # Only legitimate.png should have been written
        created_files = [f.name for f in images_dir.iterdir()]
        assert "legitimate.png" in created_files
        assert "evil.png" not in created_files
        assert "passwd" not in created_files
        assert len(created_files) == 1
        assert (images_dir / "legitimate.png").read_bytes() == b"VALID_IMAGE_BYTES"

    def test_datalab_handles_base64_data_in_images_dict(self, tmp_path):
        """Legitimate base64 string payloads in images_dict unpack properly."""
        conv = DatalabConverter()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        raw_bytes = b"IMAGE_DATA_123"
        b64_str = base64.b64encode(raw_bytes).decode("ascii")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.images = {
            "figure_b64.png": f"data:image/png;base64,{b64_str}",
        }
        mock_result.json = None
        mock_result.markdown = "# Doc"
        mock_result.runtime = 1.0
        mock_result.page_count = 1
        mock_result.parse_quality_score = 4.5

        mock_client = MagicMock()
        mock_client.convert.return_value = mock_result

        with (
            patch("zocrates.config.get_datalab_api_key", return_value="dummy-key"),
            patch("datalab_sdk.DatalabClient", return_value=mock_client),
        ):
            input_file = tmp_path / "test.pdf"
            input_file.write_bytes(b"%PDF-1.4 dummy")
            conv._convert_internal("TEST_KEY", input_file, "application/pdf", cache_dir)

        saved = cache_dir / "images" / "figure_b64.png"
        assert saved.exists()
        assert saved.read_bytes() == raw_bytes


# ---------------------------------------------------------------------------
# 3. ZoteroDB _resolve_attachment_path Security Tests
# ---------------------------------------------------------------------------


class TestZoteroDbAttachmentPathSecurity:
    """Verify ZoteroDB._resolve_attachment_path blocks path traversal in attachment references."""

    def test_resolve_attachment_path_blocks_storage_traversal(self, tmp_path):
        """storage:../../etc/passwd must be blocked and return None."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        sqlite_file = tmp_path / "zotero.sqlite"
        sqlite_file.touch()
        db = ZoteroDB(sqlite_file, storage_dir)
        item_key = "ITEM1234"

        # Traversal attempts with storage: prefix
        assert db._resolve_attachment_path(item_key, "storage:../../etc/passwd") is None
        assert db._resolve_attachment_path(item_key, "storage:../../../secret.pdf") is None
        assert db._resolve_attachment_path(item_key, "storage:subdir/../../secret.pdf") is None
        assert db._resolve_attachment_path(item_key, "storage:..") is None
        assert db._resolve_attachment_path(item_key, "storage:.") is None
        assert db._resolve_attachment_path(item_key, "storage:") is None

    def test_resolve_attachment_path_blocks_relative_traversal(self, tmp_path):
        """Relative path escaping storage/<item_key> must be blocked and return None."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        sqlite_file = tmp_path / "zotero.sqlite"
        sqlite_file.touch()
        db = ZoteroDB(sqlite_file, storage_dir)
        item_key = "ITEM1234"

        # Traversal attempts without storage: prefix
        assert db._resolve_attachment_path(item_key, "../../etc/passwd") is None
        assert db._resolve_attachment_path(item_key, "../other_item/doc.pdf") is None
        assert db._resolve_attachment_path(item_key, "sub/../../secret.pdf") is None

    def test_resolve_attachment_path_allows_legitimate_paths(self, tmp_path):
        """Legitimate storage, relative, and absolute paths must resolve correctly."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        sqlite_file = tmp_path / "zotero.sqlite"
        sqlite_file.touch()
        db = ZoteroDB(sqlite_file, storage_dir)
        item_key = "ITEM1234"
        expected_dir = (storage_dir / item_key).resolve()

        # Normal storage: path
        p1 = db._resolve_attachment_path(item_key, "storage:paper.pdf")
        assert p1 == expected_dir / "paper.pdf"
        assert p1.is_relative_to(expected_dir)

        # Normal relative path within item dir
        p2 = db._resolve_attachment_path(item_key, "paper.pdf")
        assert p2 == expected_dir / "paper.pdf"
        assert p2.is_relative_to(expected_dir)

        # Normal relative path in subfolder
        p3 = db._resolve_attachment_path(item_key, "nested/paper.pdf")
        assert p3 == expected_dir / "nested/paper.pdf"
        assert p3.is_relative_to(expected_dir)

        # Absolute path (linked file attachment)
        abs_target = Path("/tmp/linked_papers/document.pdf")
        p4 = db._resolve_attachment_path(item_key, str(abs_target))
        assert p4 == abs_target

        # Empty raw_path returns best-effort storage item directory
        p5 = db._resolve_attachment_path(item_key, "")
        assert p5 == storage_dir / item_key

    def test_resolve_attachment_path_allows_symlinked_storage_files(self, tmp_path):
        """Symlinked storage files (e.g. Zotero/ZotFile symlinked PDFs) must be preserved."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        sqlite_file = tmp_path / "zotero.sqlite"
        sqlite_file.touch()
        db = ZoteroDB(sqlite_file, storage_dir)
        item_key = "ITEM1234"

        item_dir = storage_dir / item_key
        item_dir.mkdir()

        # External file outside storage (e.g., Dropbox or user documents)
        external_dir = tmp_path / "dropbox"
        external_dir.mkdir()
        external_pdf = external_dir / "real_paper.pdf"
        external_pdf.write_bytes(b"%PDF-1.5 real paper")

        # Symlink inside item_dir pointing to external_pdf
        symlinked_pdf = item_dir / "paper.pdf"
        symlinked_pdf.symlink_to(external_pdf)

        resolved = db._resolve_attachment_path(item_key, "storage:paper.pdf")
        assert resolved == symlinked_pdf
        assert resolved.exists()
        assert resolved.read_bytes() == b"%PDF-1.5 real paper"


# ---------------------------------------------------------------------------
# 4. VLM Captioner Image Resolution Security Tests
# ---------------------------------------------------------------------------


class TestVLMCaptionerPathContainmentSecurity:
    """Verify normalize_image_relative_path rejects/sanitizes traversal outside bundle_dir."""

    def test_resolve_image_paths_rejects_relative_traversal(self, tmp_path):
        """image_ref with ../../ escaping bundle_dir must be confined to bundle_dir."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        rel_path, abs_path = normalize_image_relative_path("../../etc/passwd", bundle_dir)

        # Resolved abs_path must be strictly contained in bundle_dir
        assert abs_path.is_relative_to(bundle_dir.resolve())
        assert abs_path != Path("/etc/passwd").resolve()
        assert abs_path.name == "__traversal_blocked__"
        assert not abs_path.exists()

        # Deep traversal
        rel_path2, abs_path2 = normalize_image_relative_path("../../../../var/log/syslog", bundle_dir)
        assert abs_path2.is_relative_to(bundle_dir.resolve())
        assert abs_path2 != Path("/var/log/syslog").resolve()
        assert abs_path2.name == "__traversal_blocked__"

    def test_resolve_image_paths_rejects_external_file_uri(self, tmp_path):
        """file:/// URI pointing outside bundle_dir must be confined to bundle_dir."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()

        rel_path, abs_path = normalize_image_relative_path("file:///etc/passwd", bundle_dir)

        assert abs_path.is_relative_to(bundle_dir.resolve())
        assert abs_path != Path("/etc/passwd").resolve()
        assert abs_path.name == "__traversal_blocked__"
        assert not abs_path.exists()

    def test_resolve_image_paths_allows_legitimate_bundle_images(self, tmp_path):
        """Legitimate images inside bundle_dir must resolve without mutation."""
        bundle_dir = tmp_path / "bundle"
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True)

        target_file = images_dir / "fig1.png"
        target_file.write_bytes(b"IMAGE_1")

        # Standard relative
        rel1, abs1 = normalize_image_relative_path("images/fig1.png", bundle_dir)
        assert rel1 == "images/fig1.png"
        assert abs1 == target_file.resolve()
        assert abs1.is_relative_to(bundle_dir.resolve())

        # file:// URI pointing inside bundle_dir
        file_uri = target_file.resolve().as_uri()
        rel2, abs2 = normalize_image_relative_path(file_uri, bundle_dir)
        assert rel2 == "images/fig1.png"
        assert abs2 == target_file.resolve()
        assert abs2.is_relative_to(bundle_dir.resolve())
