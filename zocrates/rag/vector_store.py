"""
Hybrid vector store: Chroma (semantic) + SQLite FTS5 (BM25 keyword).

Architecture
------------
- ChromaDB holds embeddings and chunk metadata for semantic (dense) search.
- An auxiliary SQLite database with an FTS5 virtual table holds the raw chunk
  text for fast full-text (BM25) keyword search.
- Uses FTS5 trigram tokenizer to preserve math expressions (LaTeX) and code.
- Results from both indices are combined with Reciprocal Rank Fusion (RRF)
  to produce a single ranked list of chunks.

Reranking hook
--------------
``search()`` accepts an optional ``reranker`` callable so a cross-encoder or
other reranker can be plugged in later without changing the call-site.
"""

import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from chromadb import Collection

from zocrates.rag.chunker import ChunkResult
from zocrates.config import (
    RETRIEVAL_FINAL_K,
    RETRIEVAL_TOP_K,
    get_collection_name,
    get_semantic_similarity_threshold,
)
from zocrates.rag.embeddings import EmbedFn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """A single retrieved chunk with provenance information."""

    chunk_text: str
    item_key: str
    item_id: int
    heading_path: list[str]
    chunk_index: int
    score: float  # Combined RRF score (higher = more relevant)
    chunk_type: str = "text"  # "frontmatter" | "figure" | "table" | "equation" | "code" | "text"
    image_path: str | None = None
    caption_source: str | None = None
    vlm_model: str | None = None
    raw_semantic_score: float | None = None  # Raw similarity score (1 - distance)
    raw_keyword_rank: int | None = None  # 1-based index position in BM25 query
    retrieval_method: str = "hybrid"  # "semantic" | "keyword" | "hybrid"
    title: str = ""
    creators: list[str] = None  # type: ignore[assignment]
    year: str | None = None
    chunk_id: str = ""
    line_start: int | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        """Ensure default attributes are initialized."""
        if self.creators is None:
            self.creators = []
        if not self.chunk_id:
            self.chunk_id = f"{self.item_key}::{self.chunk_index}"


# ---------------------------------------------------------------------------
# FTS5 Trigram helpers
# ---------------------------------------------------------------------------

# Use 'trigram' tokenizer to handle math symbols (LaTeX) and code snippets literally
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id,        -- "{item_key}::{chunk_index}"
    item_key,
    chunk_text,
    tokenize = 'trigram'
);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_meta (
    chunk_id        TEXT PRIMARY KEY,
    item_key        TEXT NOT NULL,
    item_id         INTEGER NOT NULL,
    chunk_index     INTEGER NOT NULL,
    chunk_type      TEXT NOT NULL DEFAULT 'text',
    heading         TEXT,
    parent_item_key TEXT,
    image_path      TEXT,
    caption_source  TEXT,
    vlm_model       TEXT,
    line_start      INTEGER,
    line_end        INTEGER
);
"""

_FTS_INIT_LOCK = threading.Lock()
_INITIALIZED_FTS_DBS: set[Path] = set()


def _open_fts_db(path: Path) -> sqlite3.Connection:
    """Open (or create) the FTS5 and chunk metadata SQLite databases.

    Creates the ``chunks_fts`` FTS5 virtual table (trigram tokenizer) and
    the ``chunk_meta`` table if they do not already exist. Migrates missing
    columns on ``chunk_meta`` if opened on a legacy database.

    Args:
        path: Filesystem path to the SQLite database file.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory`` set to
        ``sqlite3.Row``.
    """
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    if path not in _INITIALIZED_FTS_DBS:
        with _FTS_INIT_LOCK:
            if path not in _INITIALIZED_FTS_DBS:
                conn.execute(_FTS_SCHEMA)
                conn.execute(_META_SCHEMA)

                # Check and migrate columns on chunk_meta
                existing_cols = {
                    row["name"] for row in conn.execute("PRAGMA table_info(chunk_meta)").fetchall()
                }
                for col_name in ("image_path", "caption_source", "vlm_model", "line_start", "line_end"):
                    if col_name not in existing_cols:
                        col_type = "INTEGER" if col_name in ("line_start", "line_end") else "TEXT"
                        conn.execute(f"ALTER TABLE chunk_meta ADD COLUMN {col_name} {col_type}")

                conn.commit()
                _INITIALIZED_FTS_DBS.add(path)

    return conn


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(
        semantic_ids: list[str],
        keyword_ids: list[str],
        k: int = 60,
) -> list[tuple[str, float]]:
    """
    Combine two ranked lists of chunk IDs using Reciprocal Rank Fusion (RRF).

    Args:
        semantic_ids: Chunk IDs ordered by descending semantic similarity.
        keyword_ids: Chunk IDs ordered by ascending BM25 rank (rank 1 = best).
        k: RRF smoothing constant (higher reduces the impact of top-ranked items).

    Returns:
        List of ``(chunk_id, rrf_score)`` tuples sorted by descending score.
    """
    scores: dict[str, float] = {}

    for rank, cid in enumerate(semantic_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)

    for rank, cid in enumerate(keyword_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------


class VectorStore:
    """
    Hybrid vector + keyword search index for Zotero document chunks.
    Uses versioned Chroma collections per embedding model/dimension.
    """

    def __init__(
            self,
            chroma_dir: Path,
            fts_db_path: Path,
            embedder: EmbedFn,
    ) -> None:
        """Initialize the VectorStore.

        Args:
            chroma_dir: Directory for ChromaDB persistent storage.
            fts_db_path: Filesystem path for the SQLite FTS5 database.
            embedder: Callable that maps a list of strings to a list of
                embedding vectors and exposes ``_model_name`` and
                ``dimension`` attributes.
        """
        self._rw_lock = threading.RLock()
        self._embedder = embedder
        self._fts_path = fts_db_path
        self._client = chromadb.PersistentClient(path=str(chroma_dir))

        # Determine model name and target embedding dimension
        model_name = getattr(embedder, "_model_name", "default")
        target_dim = getattr(embedder, "dimension", 768)

        # Generate versioned collection name
        self.active_collection_name = get_collection_name(model_name, target_dim)
        logger.info(
            "Initializing VectorStore with active collection: %s (model=%s, dim=%s)",
            self.active_collection_name,
            model_name,
            target_dim,
        )

        self._collection: Collection = self._client.get_or_create_collection(
            name=self.active_collection_name,
            metadata={
                "hnsw:space": "cosine",
                "model_name": str(model_name),
                "dimension": str(target_dim),
            },
        )

    def _ensure_collection_dimension(self, actual_dim: int) -> None:
        """Verify the active Chroma collection matches actual_dim, rebinding if needed.

        Must be called while holding self._rw_lock.
        """
        model_name = getattr(self._embedder, "_model_name", "default")
        expected_name = get_collection_name(model_name, actual_dim)
        if self.active_collection_name != expected_name:
            logger.info(
                "Rebinding VectorStore collection: %s -> %s (dimension changed to %d)",
                self.active_collection_name,
                expected_name,
                actual_dim,
            )
            self.active_collection_name = expected_name
            self._collection = self._client.get_or_create_collection(
                name=expected_name,
                metadata={
                    "hnsw:space": "cosine",
                    "model_name": str(model_name),
                    "dimension": str(actual_dim),
                },
            )

    @property
    def fts_path(self) -> Path:
        """Return the path to the shared SQLite FTS database."""
        return self._fts_path

    # ---- locking -----------------------------------------------------------

    def acquire_write_lock(self) -> Any:
        """Return the store's RLock for external write operations (e.g., model swap).

        Returns:
            The ``threading.RLock`` guarding Chroma and FTS access.
        """
        return self._rw_lock

    # ---- ingestion ---------------------------------------------------------

    def ingest_document(
            self,
            item_key: str,
            item_id: int,
            chunks: list[ChunkResult],
            extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Index all chunks from a single document into Chroma and FTS5.

        Deletes any existing chunks for ``item_key`` before inserting, making
        the operation idempotent.

        Args:
            item_key: Unique Zotero item key used as the primary document ID.
            item_id: Numeric Zotero item ID stored as chunk metadata.
            chunks: Ordered list of ``ChunkResult`` objects to index.
            extra_metadata: Additional key/value pairs merged into every
                chunk's Chroma metadata dict.
        """
        if not chunks:
            return

        with self._rw_lock:
            self._delete_item(item_key)

            texts = [c.text for c in chunks]
            embeddings = self._embedder(texts, task="retrieval", prompt_name="document")
            if embeddings and len(embeddings[0]) > 0:
                self._ensure_collection_dimension(len(embeddings[0]))

            parent_item_key = (extra_metadata or {}).get("parent_item_key")
            ids = [f"{item_key}::{c.chunk_index}" for c in chunks]
            metadatas = []
            for c in chunks:
                m = {
                    "item_key": item_key,
                    "parent_item_key": parent_item_key or "",
                    "item_id": item_id,
                    "chunk_index": c.chunk_index,
                    "chunk_type": c.chunk_type,
                    "heading": c.heading_str or "",
                    "document_title": c.document_title or "",
                    "chunk_id": f"{item_key}::{c.chunk_index}",
                    "line_start": getattr(c, "line_start", 1),
                    "line_end": getattr(c, "line_end", 1),
                }
                if c.image_path:
                    m["image_path"] = c.image_path
                if c.caption_source:
                    m["caption_source"] = c.caption_source
                if c.vlm_model:
                    m["vlm_model"] = c.vlm_model
                if extra_metadata:
                    for k, v in extra_metadata.items():
                        if v is not None:
                            m[k] = v
                metadatas.append(m)

            try:
                # Chroma batch upsert
                self._collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas,
                )

                # FTS5 idempotent delete-then-insert
                with _open_fts_db(self._fts_path) as conn:
                    conn.execute("DELETE FROM chunks_fts WHERE item_key = ?", (item_key,))
                    conn.execute("DELETE FROM chunk_meta WHERE item_key = ?", (item_key,))
                    conn.executemany(
                        "INSERT INTO chunks_fts (chunk_id, item_key, chunk_text) VALUES (?, ?, ?)",
                        [(cid, item_key, text) for cid, text in zip(ids, texts, strict=False)],
                    )
                    conn.executemany(
                        """
                        INSERT INTO chunk_meta (
                            chunk_id, item_key, parent_item_key, item_id,
                            chunk_index, chunk_type, heading, image_path,
                            caption_source, vlm_model, line_start, line_end
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                cid,
                                item_key,
                                parent_item_key,
                                item_id,
                                c.chunk_index,
                                c.chunk_type,
                                c.heading_str,
                                c.image_path,
                                c.caption_source,
                                c.vlm_model,
                                getattr(c, "line_start", 1),
                                getattr(c, "line_end", 1),
                            )
                            for cid, c in zip(ids, chunks, strict=False)
                        ],
                    )
                    conn.commit()

                logger.info("Ingested %d chunks for item_key=%s", len(chunks), item_key)
            finally:
                del texts, embeddings, ids, metadatas

    def is_ingested(self, item_key: str) -> bool:
        """Check whether any chunks exist for item_key in the active collection.

        Args:
            item_key: Zotero item key to look up.

        Returns:
            ``True`` if at least one chunk is indexed, ``False`` otherwise.
        """
        with self._rw_lock:
            result = self._collection.get(where={"item_key": item_key}, limit=1)
            return bool(result["ids"])

    def get_all_ingested_item_keys(self) -> set[str]:
        """Return the set of all unique item_key strings currently ingested in the active collection.

        Returns:
            Set of unique item_key strings.
        """
        with self._rw_lock:
            result = self._collection.get(include=["metadatas"])
            metadatas = result.get("metadatas") or []
            return {m["item_key"] for m in metadatas if m and "item_key" in m}

    def get_all_item_keys(self) -> set[str]:
        """Return the set of all unique item keys indexed in the chunk_meta SQLite table.

        Returns:
            Set of unique item_key strings.
        """
        try:
            with _open_fts_db(self._fts_path) as conn:
                rows = conn.execute("SELECT DISTINCT item_key FROM chunk_meta").fetchall()
                return {row["item_key"] for row in rows if row["item_key"]}
        except Exception as exc:
            logger.error("Failed to query item keys from chunk_meta: %s", exc)
            return set()

    def delete_document(self, item_key: str) -> None:
        """Remove all chunks for the given item_key from both Chroma and FTS5.

        Args:
            item_key: Zotero item key identifying the document to delete.
        """
        self._delete_item(item_key)

    def update_metadata(self, item_key: str, metadata: dict[str, Any]) -> bool:
        """Update metadata for all chunks belonging to item_key without re-embedding.

        Args:
            item_key: Zotero item key.
            metadata: Key/value pairs to merge into existing chunk metadatas.

        Returns:
            True if chunks were found and updated, False if item was not found.
        """
        if not metadata:
            return False

        with self._rw_lock:
            res = self._collection.get(where={"item_key": item_key})
            ids = res.get("ids", [])
            metadatas = res.get("metadatas", [])

            if not ids:
                return False

            clean_metadata = {k: v for k, v in metadata.items() if v is not None}
            if not clean_metadata:
                return False

            updated_metadatas = []
            for existing_meta in metadatas:
                m = dict(existing_meta) if existing_meta else {}
                m.update(clean_metadata)
                clean_m = {k: v for k, v in m.items() if v is not None}
                updated_metadatas.append(clean_m)

            self._collection.update(ids=ids, metadatas=updated_metadatas)

            # Update chunk_meta if heading is supplied in metadata
            if "heading" in metadata:
                with _open_fts_db(self._fts_path) as conn:
                    conn.execute(
                        "UPDATE chunk_meta SET heading = ? WHERE item_key = ?",
                        (metadata["heading"], item_key),
                    )
                    conn.commit()

            logger.info("Updated metadata for %d chunks of item_key=%s", len(ids), item_key)
            return True

    def update_chunk_text_preserve_vector(self, chunk_id: str, new_text: str) -> bool:
        """Update stored document text in Chroma while explicitly preserving existing vector.

        Passing existing embeddings explicitly bypasses Chroma's internal embedding function,
        guaranteeing zero ML overhead, zero network roundtrips, and strict dimension alignment.

        Args:
            chunk_id: Canonical chunk identifier (e.g. ``"{item_key}::0"``).
            new_text: New document text for the chunk.

        Returns:
            True if chunk was found, had embeddings, and was updated; False otherwise.
        """
        if not chunk_id or new_text is None:
            return False

        with self._rw_lock:
            res = self._collection.get(ids=[chunk_id], include=["embeddings"])
            embs = res.get("embeddings")
            if embs is not None and len(embs) > 0 and len(embs[0]) > 0:
                self._collection.update(
                    ids=[chunk_id],
                    documents=[new_text],
                    embeddings=[embs[0]],
                )
                logger.info("Updated chunk text preserving vector for chunk_id=%s", chunk_id)
                return True
            return False

    # ---- search ------------------------------------------------------------

    def search(
            self,
            query: str,
            top_k: int = RETRIEVAL_TOP_K,
            final_k: int = RETRIEVAL_FINAL_K,
            item_key_filter: list[str] | None = None,
            chunk_type_filter: str | list[str] | None = None,
            exclude_chunk_ids: set[str] | list[str] | None = None,
            reranker: Callable[[str, list[str]], list[float]] | None = None,
    ) -> list[SearchResult]:
        """
        Hybrid (semantic + BM25) search with RRF fusion and raw score metrics.

        Args:
            query: Natural language query string.
            top_k: Number of candidates retrieved from each index before fusion.
            final_k: Maximum number of results to return after fusion.
            item_key_filter: If provided, restrict search to these Zotero item keys.
            chunk_type_filter: Optional chunk type or list of chunk types to filter by.
            exclude_chunk_ids: Optional set or list of chunk IDs to exclude from results.
            reranker: Optional callable ``(query, texts) -> scores`` used to
                re-rank the fused results after retrieval.

        Returns:
            List of ``SearchResult`` objects sorted by descending relevance score,
            capped at ``final_k`` entries.
        """
        if not query or not query.strip():
            return []

        with self._rw_lock:
            semantic_results = self._semantic_search_with_scores(
                query, top_k, item_key_filter, chunk_type_filter, exclude_chunk_ids
            )
            keyword_results = self._keyword_search_with_ranks(
                query, top_k, item_key_filter, chunk_type_filter, exclude_chunk_ids
            )

        semantic_ids = [cid for cid, _ in semantic_results]
        keyword_ids = [cid for cid, _ in keyword_results]

        # Reciprocal Rank Fusion collapses duplicates natively
        fused = _rrf_fuse(semantic_ids, keyword_ids)[:final_k]

        if not fused:
            return []

        # Map details for explanation metrics
        semantic_scores = dict(semantic_results)
        keyword_ranks = dict(keyword_results)

        fused_ids = [cid for cid, _ in fused]
        chunk_data = self._fetch_chunks(fused_ids)

        results: list[SearchResult] = []
        for cid, score in fused:
            if cid not in chunk_data:
                continue
            data = chunk_data[cid]

            sem_score = semantic_scores.get(cid)
            kw_rank = keyword_ranks.get(cid)

            # Determine retrieval method
            if sem_score is not None and kw_rank is not None:
                method = "hybrid"
            elif sem_score is not None:
                method = "semantic"
            else:
                method = "keyword"

            results.append(
                SearchResult(
                    chunk_text=data["text"],
                    item_key=data["item_key"],
                    item_id=data["item_id"],
                    heading_path=data["heading"].split(" > ") if data["heading"] else [],
                    chunk_index=data["chunk_index"],
                    score=score,
                    chunk_type=data.get("chunk_type", "text"),
                    image_path=data.get("image_path"),
                    caption_source=data.get("caption_source"),
                    vlm_model=data.get("vlm_model"),
                    raw_semantic_score=sem_score,
                    raw_keyword_rank=kw_rank,
                    retrieval_method=method,
                    chunk_id=cid,
                    line_start=data.get("line_start"),
                    line_end=data.get("line_end"),
                )
            )

        # Optional reranking hook
        if reranker is not None:
            texts = [r.chunk_text for r in results]
            rerank_scores = reranker(query, texts)
            for r, s in zip(results, rerank_scores, strict=False):
                r.score = s
            results.sort(key=lambda r: r.score, reverse=True)

        return results

    # ---- private -----------------------------------------------------------

    def _semantic_search_with_scores(
            self,
            query: str,
            top_k: int,
            item_key_filter: list[str] | None = None,
            chunk_type_filter: str | list[str] | None = None,
            exclude_chunk_ids: set[str] | list[str] | None = None,
            similarity_threshold: float | None = None,
    ) -> list[tuple[str, float]]:
        """Retrieve chunks by semantic similarity and return scored pairs.

        Args:
            query: Query string to embed.
            top_k: Maximum number of results to return.
            item_key_filter: Optional list of item keys to restrict the search.
            chunk_type_filter: Optional chunk type or list of chunk types to include.
            exclude_chunk_ids: Optional set or list of chunk IDs to exclude.
            similarity_threshold: Minimum cosine similarity threshold (1 - distance).
                If None, loaded from zocrates_config.json (default 0.50).

        Returns:
            List of ``(chunk_id, similarity_score)`` tuples where score is
            ``1.0 - cosine_distance``.
        """
        if not query or not query.strip():
            return []

        if similarity_threshold is None:
            similarity_threshold = get_semantic_similarity_threshold()

        try:
            query_embedding = self._embedder([query], task="retrieval", prompt_name="query")[0]
            if query_embedding:
                with self._rw_lock:
                    self._ensure_collection_dimension(len(query_embedding))
            conditions: list[dict[str, Any]] = []

            if item_key_filter:
                conditions.append({
                    "$or": [
                        {"item_key": {"$in": item_key_filter}},
                        {"parent_item_key": {"$in": item_key_filter}},
                    ]
                })

            if chunk_type_filter:
                if isinstance(chunk_type_filter, str):
                    conditions.append({"chunk_type": {"$eq": chunk_type_filter}})
                elif isinstance(chunk_type_filter, (list, set, tuple)):
                    types = list(chunk_type_filter)
                    if len(types) == 1:
                        conditions.append({"chunk_type": {"$eq": types[0]}})
                    elif len(types) > 1:
                        conditions.append({"chunk_type": {"$in": types}})

            exclude_set = set(exclude_chunk_ids) if exclude_chunk_ids else set()
            if exclude_set:
                ex_list = list(exclude_set)
                if len(ex_list) == 1:
                    conditions.append({"chunk_id": {"$ne": ex_list[0]}})
                else:
                    conditions.append({"chunk_id": {"$nin": ex_list}})

            if not conditions:
                where = None
            elif len(conditions) == 1:
                where = conditions[0]
            else:
                where = {"$and": conditions}

            result = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, max(1, self._collection.count())),
                where=where,
                include=["distances"],
            )

            ids = result["ids"][0] if result["ids"] else []
            distances = result["distances"][0] if result["distances"] else []

            # Map cosine distance to similarity score: 1.0 - distance
            # Filter by similarity threshold and exclude_set fallback
            scored = []
            for cid, dist in zip(ids, distances, strict=False):
                if exclude_set and cid in exclude_set:
                    continue
                score = 1.0 - dist
                if score >= similarity_threshold:
                    scored.append((cid, score))
            return scored
        except Exception as exc:
            logger.error("Semantic search failed: %s", exc)
            return []

    def _keyword_search_with_ranks(
            self,
            query: str,
            top_k: int,
            item_key_filter: list[str] | None = None,
            chunk_type_filter: str | list[str] | None = None,
            exclude_chunk_ids: set[str] | list[str] | None = None,
    ) -> list[tuple[str, int]]:
        """Retrieve chunks by FTS5 keyword (BM25) search and return rank pairs.

        Args:
            query: Full-text query string.
            top_k: Maximum number of results to return.
            item_key_filter: Optional list of item keys to restrict the search.
            chunk_type_filter: Optional chunk type or list of chunk types to include.
            exclude_chunk_ids: Optional set or list of chunk IDs to exclude.

        Returns:
            List of ``(chunk_id, rank)`` tuples where rank is 1-based
            (rank 1 = best BM25 match).
        """
        if not query or not query.strip():
            return []

        # Sanitize FTS5 special characters and operators
        clean_query = re.sub(r'[\*\":\(\)\-\^]', ' ', query)
        clean_query = re.sub(r"\bNEAR(/\d+)?\b", " ", clean_query, flags=re.IGNORECASE)
        clean_query = " ".join(clean_query.split()).strip()
        if not clean_query:
            return []

        try:
            where_clauses = ["chunks_fts.chunk_text MATCH ?"]
            params: list[Any] = [clean_query]

            if item_key_filter:
                placeholders = ",".join("?" * len(item_key_filter))
                where_clauses.append(
                    f"(chunk_meta.item_key IN ({placeholders}) OR chunk_meta.parent_item_key IN ({placeholders}))"
                )
                params.extend(item_key_filter)
                params.extend(item_key_filter)

            if chunk_type_filter:
                if isinstance(chunk_type_filter, str):
                    where_clauses.append("chunk_meta.chunk_type = ?")
                    params.append(chunk_type_filter)
                elif isinstance(chunk_type_filter, (list, set, tuple)):
                    types = list(chunk_type_filter)
                    if len(types) == 1:
                        where_clauses.append("chunk_meta.chunk_type = ?")
                        params.append(types[0])
                    elif len(types) > 1:
                        placeholders = ",".join("?" * len(types))
                        where_clauses.append(f"chunk_meta.chunk_type IN ({placeholders})")
                        params.extend(types)

            if exclude_chunk_ids:
                ex_list = list(exclude_chunk_ids)
                if ex_list:
                    placeholders = ",".join("?" * len(ex_list))
                    where_clauses.append(f"chunk_meta.chunk_id NOT IN ({placeholders})")
                    params.extend(ex_list)

            params.append(top_k)
            sql = f"""
                SELECT chunk_meta.chunk_id FROM chunks_fts
                JOIN chunk_meta USING (chunk_id)
                WHERE {" AND ".join(where_clauses)}
                ORDER BY rank
                LIMIT ?
            """

            with _open_fts_db(self._fts_path) as conn:
                rows = conn.execute(sql, params).fetchall()
            return [(r["chunk_id"], idx) for idx, r in enumerate(rows, start=1)]
        except Exception as exc:
            logger.error("Keyword (BM25) search failed: %s", exc)
            return []

    def _fetch_chunks(self, chunk_ids: list[str]) -> dict[str, dict]:
        """Fetch chunk texts and metadata from Chroma for the given IDs.

        Args:
            chunk_ids: List of chunk IDs in the form ``"{item_key}::{chunk_index}"``.

        Returns:
            Dict mapping chunk ID to a ``dict`` with keys ``text``, ``item_key``,
            ``item_id``, ``chunk_index``, and ``heading``.
        """
        result: dict[str, dict] = {}
        if not chunk_ids:
            return result
        try:
            chroma_result = self._collection.get(
                ids=chunk_ids,
                include=["documents", "metadatas"],
            )
            c_ids = chroma_result.get("ids") or []
            c_docs = chroma_result.get("documents") or []
            c_metas = chroma_result.get("metadatas") or []
            for cid, doc, meta in zip(c_ids, c_docs, c_metas, strict=False):
                result[cid] = {
                    "text": doc,
                    "item_key": meta.get("item_key", "") if meta else "",
                    "item_id": meta.get("item_id", 0) if meta else 0,
                    "chunk_index": meta.get("chunk_index", 0) if meta else 0,
                    "chunk_type": meta.get("chunk_type", "text") if meta else "text",
                    "heading": meta.get("heading", "") if meta else "",
                    "image_path": meta.get("image_path") if meta else None,
                    "caption_source": meta.get("caption_source") if meta else None,
                    "vlm_model": meta.get("vlm_model") if meta else None,
                    "line_start": meta.get("line_start") if meta else None,
                    "line_end": meta.get("line_end") if meta else None,
                }
        except Exception as exc:
            logger.error("Fetching chunks from Chroma failed: %s", exc)
        return result

    def get_intersecting_chunk_ids(
        self, item_key: str, start_line: int, end_line: int
    ) -> list[str]:
        """Return chunk IDs for an item key that intersect the given line range [start_line, end_line].

        Args:
            item_key: Zotero item key.
            start_line: 1-indexed start line of the range.
            end_line: 1-indexed end line of the range.

        Returns:
            List of chunk IDs in chunk_index order.
        """
        try:
            with self._rw_lock:
                with _open_fts_db(self._fts_path) as conn:
                    cursor = conn.execute(
                        """
                        SELECT chunk_id FROM chunk_meta
                        WHERE item_key = ?
                          AND line_start IS NOT NULL
                          AND line_end IS NOT NULL
                          AND line_start <= ?
                          AND line_end >= ?
                        ORDER BY chunk_index ASC
                        """,
                        (item_key, end_line, start_line),
                    )
                    return [row["chunk_id"] for row in cursor.fetchall()]
        except Exception as exc:
            logger.error(
                "Failed to query intersecting chunks for %s [%d-%d]: %s",
                item_key,
                start_line,
                end_line,
                exc,
            )
            return []

    def get_all_chunk_ids_for_item(self, item_key: str) -> list[str]:
        """Return all chunk IDs for an item key in chunk_index order.

        Args:
            item_key: Zotero item key.

        Returns:
            List of chunk IDs.
        """
        try:
            with self._rw_lock:
                with _open_fts_db(self._fts_path) as conn:
                    cursor = conn.execute(
                        "SELECT chunk_id FROM chunk_meta WHERE item_key = ? ORDER BY chunk_index ASC",
                        (item_key,),
                    )
                    return [row["chunk_id"] for row in cursor.fetchall()]
        except Exception as exc:
            logger.error("Failed to query all chunk IDs for %s: %s", item_key, exc)
            return []

    def _delete_item(self, item_key: str) -> None:
        """Remove all chunks for item_key from both the Chroma collection and FTS DB.

        Args:
            item_key: Zotero item key identifying the document to delete.

        Raises:
            Exception: Re-raises any Chroma or SQLite error after logging it.
        """
        with self._rw_lock:
            try:
                self._collection.delete(where={"item_key": item_key})
            except Exception as exc:
                logger.error("Failed to delete item %s from Chroma: %s", item_key, exc)
                raise

            try:
                with _open_fts_db(self._fts_path) as conn:
                    conn.execute("DELETE FROM chunks_fts WHERE item_key = ?", (item_key,))
                    conn.execute("DELETE FROM chunk_meta WHERE item_key = ?", (item_key,))
                    conn.commit()
            except Exception as exc:
                logger.error("Failed to delete item %s from FTS: %s", item_key, exc)
                raise

    # ---- collection management ---------------------------------------------

    def list_collections(self) -> list[dict[str, Any]]:
        """List all stored versioned Chroma collections with their metadata and chunk counts.

        Returns:
            List of dicts, each with keys ``name``, ``model_name``, ``dimension``,
            ``count``, and ``is_active``.
        """
        result: list[dict[str, Any]] = []
        try:
            raw_cols = self._client.list_collections()
            for col_entry in raw_cols:
                col_name = (
                    col_entry
                    if isinstance(col_entry, str)
                    else getattr(col_entry, "name", str(col_entry))
                )
                try:
                    if hasattr(col_entry, "count") and hasattr(col_entry, "metadata"):
                        col = col_entry
                    else:
                        col = self._client.get_collection(name=col_name)
                    count = col.count()
                    meta = col.metadata or {}
                    dim = meta.get("dimension")
                    if not dim:
                        try:
                            sample = col.get(limit=1, include=["embeddings"])
                            emb = sample.get("embeddings") if sample else None
                            if emb is not None and len(emb) > 0:
                                dim = str(len(emb[0]))
                            else:
                                dim = "unknown"
                        except Exception:
                            dim = "unknown"
                    result.append({
                        "name": col_name,
                        "model_name": meta.get("model_name", "unknown"),
                        "dimension": str(dim),
                        "count": count,
                        "is_active": (col_name == self.active_collection_name),
                    })
                except Exception as inner_exc:
                    logger.warning("Error inspecting collection %s: %s", col_name, inner_exc)
        except Exception as exc:
            logger.error("Failed to list collections: %s", exc)
        return result

    def reset_collection(self, target: str = "active") -> dict[str, Any]:
        """
        Reset/clear vector store collection(s).

        Args:
            target: Which collection(s) to reset:
                - ``"active"``: delete and recreate the active versioned collection.
                - ``"all"``: delete all Chroma collections and the FTS SQLite database.
                - ``<collection_name>``: delete a specific named collection.

        Returns:
            Dict with keys ``status``, ``target``, ``deleted_collections``,
            ``fts_reset``, and ``active_collection``.
        """
        with self._rw_lock:
            deleted_collections: list[str] = []
            fts_reset = False

            if target == "all":
                try:
                    raw_cols = self._client.list_collections()
                    for c in raw_cols:
                        c_name = c if isinstance(c, str) else getattr(c, "name", str(c))
                        self._client.delete_collection(name=c_name)
                        deleted_collections.append(c_name)
                except Exception as exc:
                    logger.error("Failed to delete all Chroma collections: %s", exc)

                if self._fts_path.exists():
                    try:
                        self._fts_path.unlink()
                        fts_reset = True
                        logger.info("Unlinked shared SQLite FTS database: %s", self._fts_path)
                    except Exception as exc:
                        logger.error("Failed to unlink FTS database: %s", exc)

                model_name = getattr(self._embedder, "_model_name", "default")
                target_dim = getattr(self._embedder, "dimension", 768)
                collection_metadata = {
                    "hnsw:space": "cosine",
                    "model_name": str(model_name),
                    "dimension": str(target_dim),
                }

                self._collection = self._client.get_or_create_collection(
                    name=self.active_collection_name,
                    metadata=collection_metadata,
                )
            elif target == "active":
                try:
                    self._client.delete_collection(name=self.active_collection_name)
                    deleted_collections.append(self.active_collection_name)
                except Exception as exc:
                    logger.warning("Failed to delete active collection %s: %s", self.active_collection_name, exc)

                model_name = getattr(self._embedder, "_model_name", "default")
                target_dim = getattr(self._embedder, "dimension", 768)
                collection_metadata = {
                    "hnsw:space": "cosine",
                    "model_name": str(model_name),
                    "dimension": str(target_dim),
                }

                self._collection = self._client.get_or_create_collection(
                    name=self.active_collection_name,
                    metadata=collection_metadata,
                )
            else:
                try:
                    self._client.delete_collection(name=target)
                    deleted_collections.append(target)
                    if target == self.active_collection_name:
                        model_name = getattr(self._embedder, "_model_name", "default")
                        target_dim = getattr(self._embedder, "dimension", 768)
                        collection_metadata = {
                            "hnsw:space": "cosine",
                            "model_name": str(model_name),
                            "dimension": str(target_dim),
                        }
                        self._collection = self._client.get_or_create_collection(
                            name=self.active_collection_name,
                            metadata=collection_metadata,
                        )
                except Exception as exc:
                    logger.error("Failed to delete specific collection %s: %s", target, exc)

            return {
                "status": "success",
                "target": target,
                "deleted_collections": deleted_collections,
                "fts_reset": fts_reset,
                "active_collection": self.active_collection_name,
            }

    def close(self) -> None:
        """Close the Chroma client and release underlying storage handles."""
        try:
            if hasattr(self._client, "close"):
                self._client.close()
        except Exception as exc:
            logger.warning("Error closing Chroma client: %s", exc)
