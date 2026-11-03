"""Unit tests for versioned vector collections and vector store reset functionality."""

from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
import pytest

from zocrates.config import get_collection_name
from zocrates.rag.vector_store import VectorStore, SearchResult


def test_get_collection_name_sanitization():
    """Verify that model names with special characters are sanitized to valid ChromaDB collection names.

    Asserts that slashes, hyphens, spaces, and other non-alphanumeric characters
    are replaced with underscores, consecutive underscores collapsed, and the
    dimension suffix appended correctly.
    """
    name1 = get_collection_name("jinaai/jina-embeddings-v5-text-nano", 768)
    assert name1 == "zotero_library_jinaai_jina_embeddings_v5_text_nano_768d"

    name2 = get_collection_name("text-embedding-3-large", 3072)
    assert name2 == "zotero_library_text_embedding_3_large_3072d"

    name3 = get_collection_name("custom model @ v1!!", 384)
    assert name3 == "zotero_library_custom_model_v1_384d"


def test_vector_store_versioned_collections(tmp_path: Path):
    """Verify that two VectorStore instances with different embedders use separate collections.

    Creates two stores sharing the same Chroma directory but different mock
    embedders (768d Jina and 384d MiniLM). Asserts each store's
    ``active_collection_name`` matches its embedder and that both collections
    coexist in the client's listing.
    """
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    # Mock Embedder 1 (768d Jina)
    mock_embedder_1 = MagicMock()
    mock_embedder_1._model_name = "jinaai/jina-embeddings-v5-text-nano"
    mock_embedder_1.dimension = 768
    mock_embedder_1.return_value = [[0.1] * 768]

    vs1 = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder_1)
    assert vs1.active_collection_name == "zotero_library_jinaai_jina_embeddings_v5_text_nano_768d"

    # Mock Embedder 2 (384d Local)
    mock_embedder_2 = MagicMock()
    mock_embedder_2._model_name = "all-MiniLM-L6-v2"
    mock_embedder_2.dimension = 384
    mock_embedder_2.return_value = [[0.2] * 384]

    vs2 = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder_2)
    assert vs2.active_collection_name == "zotero_library_all_MiniLM_L6_v2_384d"

    # Both collections exist side-by-side
    cols = vs2.list_collections()
    col_names = {c["name"] for c in cols}
    assert "zotero_library_jinaai_jina_embeddings_v5_text_nano_768d" in col_names
    assert "zotero_library_all_MiniLM_L6_v2_384d" in col_names


def test_vector_store_reset_collection(tmp_path: Path):
    """Verify reset_collection clears the target collection and the FTS database.

    Asserts that ``reset_collection(target="active")`` returns a success status
    and includes the active collection in ``deleted_collections``. Also asserts
    that ``reset_collection(target="all")`` deletes the FTS SQLite file from
    disk.
    """
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"
    fts_db_path.touch()

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.5] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    # Test reset active
    res_active = vs.reset_collection(target="active")
    assert res_active["status"] == "success"
    assert res_active["target"] == "active"
    assert vs.active_collection_name in res_active["deleted_collections"]

    # Test reset all
    res_all = vs.reset_collection(target="all")
    assert res_all["status"] == "success"
    assert res_all["fts_reset"] is True
    assert vs.active_collection_name in res_all["deleted_collections"]
    assert not fts_db_path.exists()


def test_vector_store_chunk_type_persistence(tmp_path: Path):
    """Verify chunk_type is preserved in ChromaDB and SQLite chunk_meta during ingestion and search."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768, [0.2] * 768, [0.3] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    chunks = [
        ChunkResult(
            text="---\ntype: journalArticle\ntitle: Genetics Paper\ndoi: 10.1234/gene\n---",
            heading_path=[],
            chunk_index=0,
            chunk_type="frontmatter",
        ),
        ChunkResult(
            text='<figure data-type="image">\n![Fig](img.png)\nCaption\n</figure>',
            heading_path=["Results"],
            chunk_index=1,
            chunk_type="figure",
        ),
        ChunkResult(
            text="General discussion prose.",
            heading_path=["Discussion"],
            chunk_index=2,
            chunk_type="text",
        ),
    ]

    vs.ingest_document("ITEM_CHUNK_TEST", 101, chunks)
    assert vs.is_ingested("ITEM_CHUNK_TEST") is True

    # Search and verify chunk_type is populated in SearchResult for frontmatter and figure
    results = vs.search("10.1234/gene", top_k=3)
    assert len(results) > 0
    fm_res = [r for r in results if r.chunk_type == "frontmatter"]
    assert len(fm_res) > 0
    assert fm_res[0].chunk_type == "frontmatter"
    assert fm_res[0].item_key == "ITEM_CHUNK_TEST"
    assert fm_res[0].chunk_index == 0

    results_fig = vs.search("Caption", top_k=3)
    fig_res = [r for r in results_fig if r.chunk_type == "figure"]
    assert len(fig_res) > 0
    assert fig_res[0].chunk_type == "figure"
    assert fig_res[0].item_key == "ITEM_CHUNK_TEST"

    # Directly verify chunk_meta table in SQLite FTS DB
    import sqlite3
    conn = sqlite3.connect(str(fts_db_path))
    conn.row_factory = sqlite3.Row
    meta_rows = conn.execute("SELECT chunk_id, chunk_type, chunk_index FROM chunk_meta WHERE item_key = ? ORDER BY chunk_index", ("ITEM_CHUNK_TEST",)).fetchall()
    assert len(meta_rows) == 3
    assert meta_rows[0]["chunk_type"] == "frontmatter"
    assert meta_rows[0]["chunk_index"] == 0
    assert meta_rows[1]["chunk_type"] == "figure"
    assert meta_rows[2]["chunk_type"] == "text"
    conn.close()


def test_vector_store_get_all_ingested_item_keys(tmp_path: Path):
    """Verify get_all_ingested_item_keys returns all unique ingested item keys in the active collection."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    assert vs.get_all_ingested_item_keys() == set()

    chunks = [
        ChunkResult(
            text="Document chunk 1",
            heading_path=["Intro"],
            chunk_index=0,
            chunk_type="text",
        )
    ]
    vs.ingest_document("KEY_A", 1, chunks)
    vs.ingest_document("KEY_B", 2, chunks)

    keys = vs.get_all_ingested_item_keys()
    assert keys == {"KEY_A", "KEY_B"}


def test_sqlite_chunk_meta_legacy_schema_migration(tmp_path: Path):
    """Verify legacy chunk_meta table without image/VLM columns is migrated seamlessly without data loss."""
    import sqlite3
    from zocrates.rag.vector_store import _open_fts_db

    fts_db_path = tmp_path / "legacy_fts.db"

    # Create legacy table schema
    with sqlite3.connect(str(fts_db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE chunk_meta (
                chunk_id        TEXT PRIMARY KEY,
                item_key        TEXT NOT NULL,
                item_id         INTEGER NOT NULL,
                chunk_index     INTEGER NOT NULL,
                chunk_type      TEXT NOT NULL DEFAULT 'text',
                heading         TEXT,
                parent_item_key TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO chunk_meta (chunk_id, item_key, item_id, chunk_index, chunk_type, heading, parent_item_key)
            VALUES ('LEGACY_KEY::0', 'LEGACY_KEY', 42, 0, 'text', 'Old Heading', 'PAR_KEY')
            """
        )
        conn.commit()

    # Open with _open_fts_db to trigger migration
    with _open_fts_db(fts_db_path) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(chunk_meta)").fetchall()}
        assert "image_path" in cols
        assert "caption_source" in cols
        assert "vlm_model" in cols
        assert "line_start" in cols
        assert "line_end" in cols

        # Check existing row preserved
        row = conn.execute("SELECT * FROM chunk_meta WHERE chunk_id = 'LEGACY_KEY::0'").fetchone()
        assert row["item_key"] == "LEGACY_KEY"
        assert row["item_id"] == 42
        assert row["image_path"] is None
        assert row["caption_source"] is None
        assert row["vlm_model"] is None
        assert row["line_start"] is None
        assert row["line_end"] is None


def test_vector_store_image_metadata_persistence_and_retrieval(tmp_path: Path):
    """Verify image_path, caption_source, and vlm_model are preserved and returned in SearchResult."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    chunks = [
        ChunkResult(
            text="![Figure 1](images/experiment.png)\n[Visual Summary]: Loss curves",
            heading_path=["Experiments"],
            chunk_index=0,
            chunk_type="figure",
            image_path="images/experiment.png",
            caption_source="vlm_remote",
            vlm_model="openrouter/google/gemma-4-26b-a4b-it:free",
        )
    ]

    vs.ingest_document("ITEM_VLM_SEARCH", 202, chunks)

    # 1. Verify SQLite chunk_meta storage
    with sqlite3.connect(str(fts_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM chunk_meta WHERE chunk_id = 'ITEM_VLM_SEARCH::0'").fetchone()
        assert row is not None
        assert row["image_path"] == "images/experiment.png"
        assert row["caption_source"] == "vlm_remote"
        assert row["vlm_model"] == "openrouter/google/gemma-4-26b-a4b-it:free"

    # 2. Verify SearchResult population
    results = vs.search("experiment loss curves", top_k=1)
    assert len(results) == 1
    res = results[0]
    assert res.chunk_type == "figure"
    assert res.image_path == "images/experiment.png"
    assert res.caption_source == "vlm_remote"
    assert res.vlm_model == "openrouter/google/gemma-4-26b-a4b-it:free"
    assert res.chunk_id == "ITEM_VLM_SEARCH::0"


def test_vector_store_prefiltering_and_exclusion(tmp_path: Path):
    """Verify chunk_type_filter, exclude_chunk_ids, and line numbers in SearchResult."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma_filter"
    fts_db_path = tmp_path / "fts_filter.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    # Return distinct dummy embeddings
    mock_embedder.side_effect = lambda texts, **kwargs: [[0.5] * 768 for _ in texts]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    chunks = [
        ChunkResult(
            text="--- title: Paper ---",
            heading_path=[],
            chunk_index=0,
            chunk_type="frontmatter",
            line_start=1,
            line_end=3,
        ),
        ChunkResult(
            text="Prose discussing deep reinforcement learning and neural policy.",
            heading_path=["Methods"],
            chunk_index=1,
            chunk_type="text",
            line_start=5,
            line_end=15,
        ),
        ChunkResult(
            text="![Figure 1](images/f1.png)\n[Visual Summary]: Policy loss curve",
            heading_path=["Results"],
            chunk_index=2,
            chunk_type="figure",
            image_path="images/f1.png",
            line_start=18,
            line_end=25,
        ),
    ]

    vs.ingest_document("ITEM_FILTER_TEST", 101, chunks)

    # 1. Test chunk_type_filter with single string
    fig_res = vs.search("reinforcement learning loss", chunk_type_filter="figure")
    assert len(fig_res) == 1
    assert fig_res[0].chunk_type == "figure"
    assert fig_res[0].chunk_id == "ITEM_FILTER_TEST::2"
    assert fig_res[0].line_start == 18
    assert fig_res[0].line_end == 25

    # 2. Test chunk_type_filter with list
    multi_res = vs.search("paper methods", chunk_type_filter=["frontmatter", "text"])
    assert all(r.chunk_type in ("frontmatter", "text") for r in multi_res)
    assert not any(r.chunk_type == "figure" for r in multi_res)

    # 3. Test exclude_chunk_ids
    ex_res = vs.search(
        "reinforcement learning loss",
        exclude_chunk_ids={"ITEM_FILTER_TEST::1", "ITEM_FILTER_TEST::2"},
    )
    assert not any(r.chunk_id in ("ITEM_FILTER_TEST::1", "ITEM_FILTER_TEST::2") for r in ex_res)


def test_vector_store_empty_and_sanitized_queries(tmp_path: Path):
    """Verify empty queries return [] and FTS5 special characters are sanitized."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma_sanitize"
    fts_db_path = tmp_path / "fts_sanitize.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.side_effect = lambda texts, **kwargs: [[0.2] * 768 for _ in texts]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    chunks = [
        ChunkResult(
            text="Machine learning with transformers and attention mechanisms.",
            heading_path=["Introduction"],
            chunk_index=0,
            chunk_type="text",
            line_start=1,
            line_end=10,
        )
    ]
    vs.ingest_document("ITEM_SAN", 55, chunks)

    # Empty query variations should short-circuit and return []
    assert vs.search("") == []
    assert vs.search("   ") == []
    assert vs.search("\t\n") == []

    # Queries with FTS5 special characters should not crash
    res = vs.search("transformers NEAR/2 attention: (test) * -bad")
    assert isinstance(res, list)


def test_vector_store_similarity_threshold_and_sqlite_persistence(tmp_path: Path):
    """Verify cosine similarity threshold drops low-scoring matches and SQLite stores line numbers."""
    chroma_dir = tmp_path / "chroma_thresh"
    fts_db_path = tmp_path / "fts_thresh.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768

    # When ingesting (prompt_name="document"), return doc vector [1.0, 0.0, ...]
    # When querying (prompt_name="query"), return orthogonal vector [0.0, 1.0, ...]
    def fake_embed(texts, prompt_name="document", **kwargs):
        if prompt_name == "query":
            return [[0.0, 1.0] + [0.0] * 766 for _ in texts]
        return [[1.0, 0.0] + [0.0] * 766 for _ in texts]

    mock_embedder.side_effect = fake_embed

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    from zocrates.rag.chunker import ChunkResult

    chunks = [
        ChunkResult(
            text="Highly relevant text about astrophysics.",
            heading_path=["Astrophysics"],
            chunk_index=0,
            chunk_type="text",
            line_start=12,
            line_end=24,
        )
    ]
    vs.ingest_document("ITEM_ASTRO", 77, chunks)

    # Verify SQLite chunk_meta persisted line_start and line_end values directly
    with sqlite3.connect(fts_db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM chunk_meta WHERE chunk_id = 'ITEM_ASTRO::0'").fetchone()
        assert row is not None
        assert row["line_start"] == 12
        assert row["line_end"] == 24

    # Orthogonal vectors have cosine distance ~1.0 -> cosine similarity ~0.0
    # 1. Default threshold (0.50) must drop this chunk
    dropped_res = vs._semantic_search_with_scores(
        "query", top_k=5, similarity_threshold=0.50
    )
    assert len(dropped_res) == 0

    # 2. Threshold of 0.0 or lower must include it
    kept_res = vs._semantic_search_with_scores(
        "query", top_k=5, similarity_threshold=0.0
    )
    assert len(kept_res) == 1
    assert kept_res[0][0] == "ITEM_ASTRO::0"
    assert kept_res[0][1] <= 0.05


def test_vector_store_rebinding_on_dimension_auto_detection(tmp_path: Path):
    """Verify VectorStore automatically rebinds its collection if actual dimension differs from initial assumption."""
    from zocrates.rag.chunker import ChunkResult

    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    # Embedder initially assumed to be 1024 dimensions
    mock_embedder = MagicMock()
    mock_embedder._model_name = "test_cloud_model"
    mock_embedder.dimension = 1024

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)
    assert "1024d" in vs.active_collection_name

    # First real ingestion produces 768d vectors (e.g. model was actually 768d)
    mock_embedder.return_value = [[0.1] * 768]
    mock_embedder.dimension = 768

    chunks = [
        ChunkResult(
            text="Content that was embedded as 768d.",
            heading_path=["Section 1"],
            chunk_index=0,
            chunk_type="text",
            line_start=1,
            line_end=5,
        )
    ]
    vs.ingest_document("REBIND", 99, chunks)

    # Collection must have dynamically rebound to 768d
    assert "768d" in vs.active_collection_name
    assert vs._collection.metadata["dimension"] == "768"

    # Verification: search query with 768d vector succeeds on the rebound collection
    results = vs._semantic_search_with_scores("query", top_k=5, similarity_threshold=0.0)
    assert len(results) == 1
    assert results[0][0] == "REBIND::0"


def test_vector_store_fetch_chunks_empty(tmp_path: Path):
    """Verify VectorStore._fetch_chunks([]) safely returns empty dict without Chroma ValueError."""
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)
    res = vs._fetch_chunks([])
    assert res == {}


def test_vector_store_list_collections_fallback_dimension_from_peek(tmp_path: Path):
    """Verify list_collections() handles missing dimension metadata by inspecting peek embeddings."""
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)

    # Manually create a collection without 'dimension' in metadata
    raw_col = vs._client.create_collection("legacy_col_no_dim", metadata={"model_name": "legacy"})
    raw_col.add(ids=["c1"], documents=["test doc"], embeddings=[[0.1] * 512])

    cols = vs.list_collections()
    col_by_name = {c["name"]: c for c in cols}
    assert "legacy_col_no_dim" in col_by_name
    assert col_by_name["legacy_col_no_dim"]["dimension"] == "512"


def test_vector_store_close(tmp_path: Path):
    """Verify VectorStore.close() executes cleanly and releases client handles."""
    chroma_dir = tmp_path / "chroma"
    fts_db_path = tmp_path / "fts_index.db"

    mock_embedder = MagicMock()
    mock_embedder._model_name = "test-model"
    mock_embedder.dimension = 768
    mock_embedder.return_value = [[0.1] * 768]

    vs = VectorStore(chroma_dir=chroma_dir, fts_db_path=fts_db_path, embedder=mock_embedder)
    # Should close without raising any exceptions
    vs.close()





