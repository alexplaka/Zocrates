"""Unit tests for Zocrates data directory portability and path relativization."""

import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from zocrates.agents.common.toolkit import search_corpus_core
from zocrates.rag.bundle_catalog import (
    _compute_relative_source_path,
    build_frontmatter,
    generate_root_index_md,
    normalize_to_storage_relative,
    resolve_source_path,
)
from zocrates.rag.document_processor import (
    DocumentProcessor,
    _migrate_storage_paths_to_relative,
    _open_meta_db,
    _relativize_staging_markdown_links,
    _sanitize_cached_bundle_image_links,
)
from zocrates.rag.vector_store import SearchResult
from scripts.refresh_frontmatter import refresh_frontmatters


class TestNormalizeToStorageRelative:
    """Verify normalize_to_storage_relative across Unix, Windows, URIs, and external paths."""

    def test_posix_storage_path(self) -> None:
        raw = "/home/alex/Zotero/storage/WNBK68IW/paper.pdf"
        assert normalize_to_storage_relative(raw) == "storage/WNBK68IW/paper.pdf"

    def test_windows_storage_path(self) -> None:
        raw = "C:\\Users\\alex\\Zotero\\storage\\WNBK68IW\\paper.pdf"
        assert normalize_to_storage_relative(raw) == "storage/WNBK68IW/paper.pdf"

    def test_url_encoded_spaces_and_file_uri(self) -> None:
        raw = "file:///home/alex/Zotero/storage/WNBK68IW/my%20paper%20file.pdf"
        assert normalize_to_storage_relative(raw) == "storage/WNBK68IW/my paper file.pdf"

    def test_directory_attachment(self) -> None:
        raw = "/home/alex/Zotero/storage/4ZTGFTT7"
        assert normalize_to_storage_relative(raw) == "storage/4ZTGFTT7"

    def test_zotero_note_uri(self) -> None:
        raw = "zotero://note/NOTE123"
        assert normalize_to_storage_relative(raw) == "zotero://note/NOTE123"

    def test_remote_url(self) -> None:
        raw = "https://arxiv.org/pdf/1706.03762.pdf"
        assert normalize_to_storage_relative(raw) == "https://arxiv.org/pdf/1706.03762.pdf"

    def test_external_linked_file(self) -> None:
        raw = "/home/alex/Documents/MySpecialPaper.pdf"
        assert normalize_to_storage_relative(raw) == "/home/alex/Documents/MySpecialPaper.pdf"

    def test_empty_and_none(self) -> None:
        assert normalize_to_storage_relative("") == ""
        assert normalize_to_storage_relative(None) == ""
        assert normalize_to_storage_relative("   ") == ""


class TestResolveSourcePath:
    """Verify resolve_source_path local re-anchoring and URI passthrough."""

    def test_zotero_note_passthrough(self, tmp_path) -> None:
        res = resolve_source_path("zotero://note/NOTE123", tmp_path)
        assert res == "zotero://note/NOTE123"

    def test_storage_relative_resolution(self, tmp_path) -> None:
        profile_dir = tmp_path / "Zotero"
        storage_file = profile_dir / "storage" / "ITEM1" / "doc.pdf"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text("dummy", encoding="utf-8")

        res = resolve_source_path("storage/ITEM1/doc.pdf", profile_dir)
        assert res == storage_file.resolve()

    def test_storage_relative_with_url_encoding(self, tmp_path) -> None:
        profile_dir = tmp_path / "Zotero"
        storage_file = profile_dir / "storage" / "ITEM1" / "my doc.pdf"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text("dummy", encoding="utf-8")

        res = resolve_source_path("storage/ITEM1/my%20doc.pdf", profile_dir)
        assert res == storage_file.resolve()

    def test_reanchor_foreign_machine_legacy_path(self, tmp_path) -> None:
        profile_dir = tmp_path / "Zotero"
        local_file = profile_dir / "storage" / "REANCHOR1" / "paper.pdf"
        local_file.parent.mkdir(parents=True)
        local_file.write_text("content", encoding="utf-8")

        foreign_path = "/home/foreign_user/Zotero/storage/REANCHOR1/paper.pdf"
        res = resolve_source_path(foreign_path, profile_dir)
        assert res == local_file.resolve()

    def test_external_existing_path(self, tmp_path) -> None:
        external_file = tmp_path / "external.pdf"
        external_file.write_text("ext", encoding="utf-8")
        res = resolve_source_path(str(external_file), tmp_path)
        assert res == external_file.resolve()


class TestStoragePathsMigration:
    """Verify self-healing database migration user_version=0 -> 1 with pre-migration backup."""

    def test_migration_upgrades_schema_and_backs_up(self, tmp_path) -> None:
        db_path = tmp_path / "conversion_meta.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE conversion_meta (
                item_key TEXT PRIMARY KEY,
                source_path TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO conversion_meta (item_key, source_path) VALUES (?, ?);",
            [
                ("KEY1", "/home/foreign_user/Zotero/storage/KEY1/paper.pdf"),
                ("KEY2", "C:\\Users\\foreign\\storage\\KEY2\\chapter.pdf"),
                ("NOTE1", "zotero://note/NOTE1"),
                ("ALREADY1", "storage/ALREADY1/done.pdf"),
                ("GUARD1", "/home/foreign/storage/MISMATCH/paper.pdf"),  # Key mismatch guard
            ],
        )
        conn.commit()
        conn.close()

        # Run migration
        _migrate_storage_paths_to_relative(db_path)

        # Check backup was created
        bak_path = tmp_path / "conversion_meta.db.bak-pre-relativization"
        assert bak_path.exists()

        # Check migrated rows
        check_conn = sqlite3.connect(str(db_path))
        check_conn.row_factory = sqlite3.Row
        ver = check_conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 1

        rows = {r["item_key"]: r["source_path"] for r in check_conn.execute("SELECT * FROM conversion_meta").fetchall()}
        assert rows["KEY1"] == "storage/KEY1/paper.pdf"
        assert rows["KEY2"] == "storage/KEY2/chapter.pdf"
        assert rows["NOTE1"] == "zotero://note/NOTE1"
        assert rows["ALREADY1"] == "storage/ALREADY1/done.pdf"
        # Guard1 was protected from false positive rewrite
        assert rows["GUARD1"] == "/home/foreign/storage/MISMATCH/paper.pdf"
        check_conn.close()

        # Run a second time -- must be idempotent
        _migrate_storage_paths_to_relative(db_path)
        check_conn2 = sqlite3.connect(str(db_path))
        ver2 = check_conn2.execute("PRAGMA user_version;").fetchone()[0]
        assert ver2 == 1
        check_conn2.close()


class TestSanitizeCachedBundleImageLinks:
    """Verify intra-line replacement of file:///.../images/ with images/ and line count preservation."""

    def test_sanitize_holdover_bundles(self, tmp_path) -> None:
        cache_dir = tmp_path / "corpus"
        item_dir = cache_dir / "ITEM_HOLDOVER"
        item_dir.mkdir(parents=True)

        original_lines = [
            "# Test Document\n",
            "\n",
            "<figure data-type=\"image\">\n",
            "\n",
            "![Figure 1](file:///home/alex/Zotero/Zotero%20test-user/zocrates-data/markdown_cache/ITEM_HOLDOVER/images/picture_000.png)\n",
            "\n",
            "**Figure 1.** Sample caption\n",
            "</figure>\n",
        ]
        doc_path = item_dir / "document.md"
        doc_path.write_text("".join(original_lines), encoding="utf-8")

        count = _sanitize_cached_bundle_image_links(cache_dir)
        assert count == 1

        new_text = doc_path.read_text(encoding="utf-8")
        assert "file://" not in new_text
        assert "![Figure 1](images/picture_000.png)" in new_text

        # Verify line count preserved exactly
        assert len(new_text.splitlines(keepends=True)) == len(original_lines)

    def test_relativize_staging_markdown_links(self, tmp_path) -> None:
        staging_dir = tmp_path / "staging_123"
        staging_dir.mkdir()

        md_input = (
            f"Here is an image: ![]({staging_dir.as_uri()}/images/pic.png)\n"
            f"And path: ![]({staging_dir}/images/pic2.png)\n"
            f"And leftover: ![](file:///tmp/other/images/pic3.png)\n"
        )
        cleaned = _relativize_staging_markdown_links(md_input, staging_dir)
        assert cleaned == (
            "Here is an image: ![](images/pic.png)\n"
            "And path: ![](images/pic2.png)\n"
            "And leftover: ![](images/pic3.png)\n"
        )


class TestItemIdDriftGuard:
    """Verify search_corpus_core Tier 2 protection against cross-sync item_id drift."""

    def test_key_authoritative_resolution_on_id_mismatch(self) -> None:
        mock_syncer = MagicMock()
        mock_store = MagicMock()
        mock_db = MagicMock()
        mock_syncer.store = mock_store
        mock_syncer.db = mock_db

        fake_res = SearchResult(
            item_id=99,
            item_key="TARGET_KEY",
            chunk_index=0,
            chunk_text="Some text",
            heading_path=[],
            score=0.1,
            raw_semantic_score=0.9,
            raw_keyword_rank=1,
            retrieval_method="hybrid",
            chunk_type="text",
            image_path=None,
            caption_source=None,
            vlm_model=None,
            chunk_id="TARGET_KEY::0",
            line_start=1,
            line_end=5,
        )
        mock_store.search.return_value = [fake_res]

        # ID 99 returns an item with a drifted/different key!
        wrong_cand = MagicMock()
        wrong_cand.item_key = "WRONG_KEY"
        wrong_cand.title = "Stale Wrong Title"
        mock_db.get_item.return_value = wrong_cand

        # Authoritative lookup by key returns the correct item
        correct_cand = MagicMock()
        correct_cand.item_key = "TARGET_KEY"
        correct_cand.title = "Correct Authoritative Title"
        mock_db.get_item_by_key.return_value = correct_cand

        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = search_corpus_core(query="test", limit=5)

        assert res["status"] == "success"
        assert res["results"][0]["title"] == "Correct Authoritative Title"
        mock_db.get_item_by_key.assert_called_with("TARGET_KEY")


class TestSimulatedCrossMachineMove:
    """Verify copy-paste portability by creating artifacts on Machine A and opening on Machine B."""

    def test_cross_machine_seamless_reading_and_refresh(self, tmp_path) -> None:
        machine_a = tmp_path / "machine_a"
        machine_b = tmp_path / "machine_b"

        # 1. Setup Machine A
        a_profile = machine_a / "Zotero"
        a_cache = a_profile / "zocrates-data" / "corpus"
        a_storage = a_profile / "storage" / "PORTABLE1"
        a_storage.mkdir(parents=True)
        source_doc = a_storage / "portable_paper.txt"
        source_doc.write_text("Portable paper content line 1\nLine 2\n", encoding="utf-8")

        proc_a = DocumentProcessor(a_cache)
        converted = proc_a.get_or_convert(
            item_key="PORTABLE1",
            source_path=source_doc,
            content_type="text/plain",
            parent_item_type="journalArticle",
            title="Portable Paper",
        )
        assert converted is not None

        # Verify DB on Machine A has storage-relative path
        with _open_meta_db(a_cache) as conn:
            row = conn.execute("SELECT * FROM conversion_meta WHERE item_key = 'PORTABLE1'").fetchone()
            assert row["source_path"] == "storage/PORTABLE1/portable_paper.txt"

        # Verify document.md frontmatter on Machine A has ../../../storage/...
        doc_a = (a_cache / "PORTABLE1" / "document.md").read_text(encoding="utf-8")
        parsed_fm = yaml.safe_load(doc_a.split("---")[1])
        assert parsed_fm["source"]["path"] == "../../../storage/PORTABLE1/portable_paper.txt"

        # 2. Simulate Copy-Paste Move to Machine B
        b_profile = machine_b / "Zotero"
        b_storage = b_profile / "storage" / "PORTABLE1"
        b_storage.mkdir(parents=True)
        (b_storage / "portable_paper.txt").write_text(source_doc.read_text(encoding="utf-8"), encoding="utf-8")

        # Copy the entire zocrates-data folder to Machine B
        b_cache = b_profile / "zocrates-data" / "corpus"
        shutil.copytree(a_cache.parent, b_profile / "zocrates-data")

        # 3. Open on Machine B with DocumentProcessor
        proc_b = DocumentProcessor(b_cache)
        assert proc_b.is_cached("PORTABLE1")

        # Regenerate root catalog on Machine B
        generate_root_index_md(b_cache)
        root_index_b = (b_cache / "index.md").read_text(encoding="utf-8")
        assert "machine_a" not in root_index_b
        assert "[Source Document](../../storage/PORTABLE1/portable_paper.txt)" in root_index_b

        # Verify cached document can be read cleanly
        content_b = proc_b.read_cached("PORTABLE1")
        assert content_b is not None
        assert "Portable paper content line 1" in content_b
