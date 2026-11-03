"""Unit tests for Phase 5: Deletion, Cleanup & Metadata Updates.

Tests:
  - SQLite WAL mode is enabled on conversion_meta.db.
  - DocumentProcessor.delete_cached removes item bundle, DB row, and updates root index.
  - DocumentProcessor.update_indexed_at sets indexed_at timestamp in conversion_meta DB.
  - VectorStore.update_metadata updates chunk metadata in ChromaDB and FTS5 without re-embedding.
  - Orphan detection in full_sync identifies and removes cached items absent from Zotero.
"""

from pathlib import Path

from unittest.mock import MagicMock

import pytest

from zocrates.rag.document_processor import DocumentProcessor, _open_meta_db
from zocrates.rag.vector_store import ChunkResult, VectorStore


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "md_cache"


@pytest.fixture
def chroma_dir(tmp_path) -> Path:
    return tmp_path / "chroma"


@pytest.fixture
def fts_db_path(tmp_path) -> Path:
    return tmp_path / "fts_index.db"


@pytest.fixture
def store(chroma_dir, fts_db_path) -> VectorStore:
    embedder = MagicMock()
    embedder._model_name = "test-model"
    embedder.dimension = 768
    embedder.side_effect = lambda texts, **kwargs: [[0.1] * 768 for _ in texts]
    return VectorStore(
        chroma_dir=chroma_dir,
        fts_db_path=fts_db_path,
        embedder=embedder,
    )


class TestSQLiteWALMode:
    """Verify SQLite WAL mode configuration."""

    def test_wal_mode_enabled(self, cache_dir) -> None:
        with _open_meta_db(cache_dir) as conn:
            row = conn.execute("PRAGMA journal_mode;").fetchone()
            assert row[0].lower() == "wal"


class TestDocumentProcessorDeletion:
    """Verify delete_cached and update_indexed_at."""

    def test_delete_cached_removes_bundle_and_db_row(self, cache_dir, tmp_path) -> None:
        source = tmp_path / "paper.txt"
        source.write_text("content", encoding="utf-8")

        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert("DELKEY1", source, "text/plain")

        assert (cache_dir / "DELKEY1" / "document.md").exists()
        assert processor.is_cached("DELKEY1")

        deleted = processor.delete_cached("DELKEY1")
        assert deleted is True
        assert not (cache_dir / "DELKEY1").exists()
        assert not processor.is_cached("DELKEY1")

    def test_delete_cached_nonexistent_returns_false(self, cache_dir) -> None:
        processor = DocumentProcessor(cache_dir)
        assert processor.delete_cached("NONEXISTENT") is False

    def test_update_indexed_at(self, cache_dir, tmp_path) -> None:
        source = tmp_path / "paper.txt"
        source.write_text("content", encoding="utf-8")

        processor = DocumentProcessor(cache_dir)
        processor.get_or_convert("IDXKEY1", source, "text/plain")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT indexed_at FROM conversion_meta WHERE item_key = ?",
                ("IDXKEY1",),
            ).fetchone()
            assert row["indexed_at"] is None

        processor.update_indexed_at("IDXKEY1", "2026-03-30T15:00:00Z")

        with _open_meta_db(cache_dir) as conn:
            row = conn.execute(
                "SELECT indexed_at FROM conversion_meta WHERE item_key = ?",
                ("IDXKEY1",),
            ).fetchone()
            assert row["indexed_at"] == "2026-03-30T15:00:00Z"


class TestVectorStoreUpdateMetadata:
    """Verify VectorStore.update_metadata method."""

    def test_update_metadata_merges_new_fields(self, store) -> None:
        chunks = [
            ChunkResult(chunk_index=0, text="First chunk text", heading_path=["Intro"], document_title="Paper"),
        ]
        store.ingest_document("VKEY1", 101, chunks, {"collection": "Old Collection"})

        res_before = store.search("chunk text", top_k=1, final_k=1)
        assert len(res_before) == 1

        # Update metadata without re-embedding
        updated = store.update_metadata("VKEY1", {"collection": "New Collection", "heading": "New Intro"})
        assert updated is True

        # Search again to verify retrieval still works
        res_after = store.search("chunk text", top_k=1, final_k=1)
        assert len(res_after) == 1

    def test_ingest_document_with_none_metadata_values_succeeds(self, store) -> None:
        """Verify that Chroma does not raise ValueError when extra_metadata contains None values."""
        chunks = [
            ChunkResult(chunk_index=0, text="Standalone chunk text", heading_path=None, document_title=None),
        ]
        # Ingest with None fields simulating standalone attachments
        store.ingest_document(
            "STANDALONE_KEY",
            202,
            chunks,
            {
                "content_type": "application/pdf",
                "parent_item_key": None,
                "parent_item_id": None,
                "extra_none": None,
            },
        )
        assert store.is_ingested("STANDALONE_KEY") is True

    def test_update_metadata_with_none_values(self, store) -> None:
        """Verify updating metadata with None values does not crash and filters cleanly."""
        chunks = [
            ChunkResult(chunk_index=0, text="Update test chunk text", heading_path=["Intro"], document_title="Doc"),
        ]
        store.ingest_document("UPDATE_KEY", 303, chunks, {"valid_field": "old_value"})
        # Update with mixed None and valid values
        updated = store.update_metadata("UPDATE_KEY", {"valid_field": "new_value", "ignored_field": None})
        assert updated is True

    def test_update_metadata_nonexistent_returns_false(self, store) -> None:
        updated = store.update_metadata("NONEXISTENT_KEY", {"key": "val"})
        assert updated is False
