"""Unit tests for Phase 2: Local MCP Server FastAPI Endpoints.

Covers:
- 403 Forbidden when enable_mcp_server is False across all 4 endpoints.
- 503 Service Unavailable when syncer is not initialized across all 4 endpoints.
- 400 Bad Request on invalid item key format.
- 404 Not Found on nonexistent item keys for toc, read, relations.
- 200 Success paths for /mcp/search, /mcp/documents/{key}/toc, /mcp/documents/{key}/read, and /mcp/documents/{key}/relations.
- Asset URL augmentation formatting on /mcp/documents/{key}/read.
- Limit clamping for search (limit=100 -> clamped to 20, limit<=0 -> clamped to 1).
- Ceiling clamping for relations (ceiling=100 -> clamped to 50, ceiling<=0 -> clamped to 1).
- 500 Internal Server Error when core toolkit encounters an unexpected failure.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app


@pytest.fixture
def client():
    """Test client for FastAPI app."""
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


@pytest.fixture
def mock_active_syncer():
    """Mock active syncer so get_syncer() succeeds by default."""
    syncer = MagicMock()
    syncer._zotero_data_dir = "/dummy/zotero"
    return syncer


class TestMCPPermissionGate:
    """Verify MCP permission gate and service availability checks."""

    def test_mcp_disabled_returns_403_for_all_endpoints(self, client, mock_active_syncer):
        """When MCP is disabled in preferences, all endpoints must return 403."""
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=False),
        ):
            # 1. Search
            res_search = client.post("/mcp/search", json={"query": "neural networks"})
            assert res_search.status_code == 403
            assert "MCP server access is disabled in Zocrates preferences" in res_search.json()["detail"]

            # 2. ToC
            res_toc = client.get("/mcp/documents/ITEM123/toc")
            assert res_toc.status_code == 403
            assert "MCP server access is disabled in Zocrates preferences" in res_toc.json()["detail"]

            # 3. Read
            res_read = client.post("/mcp/documents/ITEM123/read", json={})
            assert res_read.status_code == 403
            assert "MCP server access is disabled in Zocrates preferences" in res_read.json()["detail"]

            # 4. Relations
            res_rel = client.get("/mcp/documents/ITEM123/relations")
            assert res_rel.status_code == 403
            assert "MCP server access is disabled in Zocrates preferences" in res_rel.json()["detail"]

            # 5. Catalog
            res_cat = client.get("/mcp/catalog")
            assert res_cat.status_code == 403
            assert "MCP server access is disabled in Zocrates preferences" in res_cat.json()["detail"]

    def test_syncer_unregistered_returns_503_for_all_endpoints(self, client):
        """When syncer is not registered/initialized, all endpoints must return 503."""
        with (
            patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not initialized")),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
        ):
            # 1. Search
            res_search = client.post("/mcp/search", json={"query": "neural networks"})
            assert res_search.status_code == 503
            assert "Zotero library is not yet registered or synced" in res_search.json()["detail"]

            # 2. ToC
            res_toc = client.get("/mcp/documents/ITEM123/toc")
            assert res_toc.status_code == 503
            assert "Zotero library is not yet registered or synced" in res_toc.json()["detail"]

            # 3. Read
            res_read = client.post("/mcp/documents/ITEM123/read", json={})
            assert res_read.status_code == 503
            assert "Zotero library is not yet registered or synced" in res_read.json()["detail"]

            # 4. Relations
            res_rel = client.get("/mcp/documents/ITEM123/relations")
            assert res_rel.status_code == 503
            assert "Zotero library is not yet registered or synced" in res_rel.json()["detail"]

            # 5. Catalog
            res_cat = client.get("/mcp/catalog")
            assert res_cat.status_code == 503
            assert "Zotero library is not yet registered or synced" in res_cat.json()["detail"]


class TestMCPValidationErrors:
    """Verify 400 Bad Request handling for invalid item key formats."""

    @pytest.mark.parametrize(
        "invalid_key",
        ["INVALID KEY", "bad:key", "key.dot", "key$special"],
    )
    def test_invalid_item_key_returns_400(self, client, mock_active_syncer, invalid_key):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
        ):
            # ToC
            res_toc = client.get(f"/mcp/documents/{invalid_key}/toc")
            assert res_toc.status_code == 400
            assert "Invalid item key format" in res_toc.json()["detail"]

            # Read
            res_read = client.post(f"/mcp/documents/{invalid_key}/read", json={})
            assert res_read.status_code == 400
            assert "Invalid item key format" in res_read.json()["detail"]

            # Relations
            res_rel = client.get(f"/mcp/documents/{invalid_key}/relations")
            assert res_rel.status_code == 400
            assert "Invalid item key format" in res_rel.json()["detail"]

            # Search with invalid item_key string
            res_search_str = client.post(
                "/mcp/search",
                json={"query": "test", "item_key": invalid_key},
            )
            assert res_search_str.status_code == 400
            assert "Invalid item key format" in res_search_str.json()["detail"]

            # Search with invalid item_key in list
            res_search_list = client.post(
                "/mcp/search",
                json={"query": "test", "item_key": ["VALID123", invalid_key]},
            )
            assert res_search_list.status_code == 400
            assert "Invalid item key format" in res_search_list.json()["detail"]

    @pytest.mark.parametrize(
        "traversal_key",
        ["../traversal", "../../etc/passwd", "key/with/slash"],
    )
    def test_search_item_key_traversal_returns_400(self, client, mock_active_syncer, traversal_key):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
        ):
            res = client.post(
                "/mcp/search",
                json={"query": "test", "item_key": traversal_key},
            )
            assert res.status_code == 400
            assert "Invalid item key format" in res.json()["detail"]


class TestMCPNotFoundErrors:
    """Verify 404 Not Found handling for nonexistent or unconverted items."""

    def test_toc_not_found_returns_404(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_document_toc_core",
                return_value={
                    "status": "error",
                    "message": "Document bundle for MISSING not found or not yet converted.",
                },
            ),
        ):
            res = client.get("/mcp/documents/MISSING/toc")
            assert res.status_code == 404
            assert "not found or not yet converted" in res.json()["detail"]

    def test_read_not_found_returns_404(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.read_document_core",
                return_value={
                    "status": "error",
                    "message": "Document bundle for MISSING not found or not yet converted.",
                },
            ),
        ):
            res = client.post("/mcp/documents/MISSING/read", json={})
            assert res.status_code == 404
            assert "not found or not yet converted" in res.json()["detail"]

    def test_relations_not_found_returns_404(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_related_items_core",
                return_value={
                    "status": "error",
                    "message": "Document bundle for MISSING not found or not yet converted.",
                },
            ),
        ):
            res = client.get("/mcp/documents/MISSING/relations")
            assert res.status_code == 404
            assert "not found or not yet converted" in res.json()["detail"]


class TestMCPSuccessPaths:
    """Verify 200 Success paths and payload augmentation."""

    def test_mcp_search_success(self, client, mock_active_syncer):
        mock_results = [
            {
                "chunk_id": "ABC12345::0",
                "item_key": "ABC12345",
                "title": "Quantum Computing Review",
                "text": "Quantum entanglement details...",
            }
        ]
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.search_corpus_core",
                return_value={"status": "success", "results": mock_results},
            ) as mock_search,
        ):
            res = client.post(
                "/mcp/search",
                json={
                    "query": "quantum entanglement",
                    "limit": 10,
                    "item_key": "ABC12345",
                    "chunk_type": "text",
                    "exclude_chunk_ids": ["EXCLUDE1"],
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert len(data["results"]) == 1
            assert data["results"][0]["chunk_id"] == "ABC12345::0"
            mock_search.assert_called_once_with(
                query="quantum entanglement",
                item_key="ABC12345",
                chunk_type="text",
                limit=10,
                exclude_chunk_ids=["EXCLUDE1"],
            )

    def test_mcp_search_limit_clamping(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.search_corpus_core",
                return_value={"status": "success", "results": []},
            ) as mock_search,
        ):
            # Upper bound clamp: limit=100 -> clamped to 20
            client.post("/mcp/search", json={"query": "test", "limit": 100})
            assert mock_search.call_args.kwargs["limit"] == 20

            # Lower bound clamp: limit=0 -> clamped to 1
            client.post("/mcp/search", json={"query": "test", "limit": 0})
            assert mock_search.call_args.kwargs["limit"] == 1

            # In-bounds value: limit=15 -> untouched
            client.post("/mcp/search", json={"query": "test", "limit": 15})
            assert mock_search.call_args.kwargs["limit"] == 15

    def test_mcp_get_toc_success(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_document_toc_core",
                return_value={
                    "status": "success",
                    "item_key": "ITEM123",
                    "toc_content": "# Table of Contents\n- [s1] Intro",
                },
            ) as mock_toc,
        ):
            res = client.get("/mcp/documents/ITEM123/toc")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ITEM123"
            assert "Intro" in data["toc_content"]
            mock_toc.assert_called_once_with(item_key="ITEM123")

    def test_mcp_read_document_success_and_asset_url_augmentation(self, client, mock_active_syncer):
        mock_payload = {
            "status": "success",
            "item_key": "RESOLVED_KEY",
            "title": "Document Title",
            "assets": [
                {
                    "id": "f1",
                    "type": "figure",
                    "path": "images/fig1.png",
                    "content": "Figure 1 diagram",
                },
                {
                    "id": "f2",
                    "type": "figure",
                    "path": "/images/fig2.jpg",  # Leading slash normalization check
                    "content": "Figure 2 diagram",
                },
                {
                    "id": "s1",
                    "type": "section",
                    "content": "Text content without path",
                },
            ],
            "intersecting_chunk_ids": ["RESOLVED_KEY::1"],
        }
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.read_document_core",
                return_value=mock_payload,
            ) as mock_read,
        ):
            res = client.post(
                "/mcp/documents/ITEM123/read",
                json={"asset_id": "f1", "page": 1},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "RESOLVED_KEY"
            mock_read.assert_called_once_with(
                item_key="ITEM123",
                asset_id="f1",
                page=1,
            )

            # Check asset URL augmentation
            assets = data["assets"]
            assert assets[0]["url"] == "http://testserver/assets/RESOLVED_KEY/images/fig1.png"
            assert assets[1]["url"] == "http://testserver/assets/RESOLVED_KEY/images/fig2.jpg"
            assert "url" not in assets[2]

    def test_mcp_get_relations_success_and_ceiling_clamping(self, client, mock_active_syncer):
        mock_relations = [
            {
                "item_key": "TARGET_1",
                "title": "Related Paper One",
                "relation_type": "hybrid_search",
                "score": 0.85,
            }
        ]
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_related_items_core",
                return_value={
                    "status": "success",
                    "item_key": "ITEM123",
                    "total_count": 1,
                    "related_items": mock_relations,
                },
            ) as mock_relations_core,
        ):
            # Normal success path with default ceiling
            res = client.get("/mcp/documents/ITEM123/relations")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["total_count"] == 1
            mock_relations_core.assert_called_with(item_key="ITEM123", ceiling=25)

            # Upper bound clamp: ceiling=100 -> clamped to 50
            client.get("/mcp/documents/ITEM123/relations?ceiling=100")
            assert mock_relations_core.call_args.kwargs["ceiling"] == 50

            # Lower bound clamp: ceiling=0 -> clamped to 1
            client.get("/mcp/documents/ITEM123/relations?ceiling=0")
            assert mock_relations_core.call_args.kwargs["ceiling"] == 1

            # In-bounds custom ceiling: ceiling=35 -> 35
            client.get("/mcp/documents/ITEM123/relations?ceiling=35")
            assert mock_relations_core.call_args.kwargs["ceiling"] == 35

    def test_mcp_get_catalog_existing_index(self, client, mock_active_syncer, tmp_path):
        """Catalog endpoint returns existing index.md content without regeneration."""
        cache_dir = tmp_path / "corpus"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / "index.md"
        index_file.write_text("# Existing Catalog\n\nContent here", encoding="utf-8")

        mock_active_syncer.processor._cache_dir = cache_dir

        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch("zocrates.fast_api_app.generate_root_index_md") as mock_gen,
        ):
            res = client.get("/mcp/catalog")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["content"] == "# Existing Catalog\n\nContent here"
            mock_gen.assert_not_called()

    def test_mcp_get_catalog_generates_when_missing(self, client, mock_active_syncer, tmp_path):
        """Catalog endpoint generates index.md when it does not exist."""
        cache_dir = tmp_path / "corpus"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / "index.md"

        mock_active_syncer.processor._cache_dir = cache_dir

        def fake_generate(cd):
            (cd / "index.md").write_text("# Generated Catalog\n", encoding="utf-8")

        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch("zocrates.fast_api_app.generate_root_index_md", side_effect=fake_generate) as mock_gen,
        ):
            res = client.get("/mcp/catalog")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["content"] == "# Generated Catalog\n"
            mock_gen.assert_called_once_with(cache_dir)


class TestMCPInternalErrors:
    """Verify 500 Internal Server Error when core toolkit encounters an unexpected failure."""

    def test_search_core_failure_returns_500(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.search_corpus_core",
                return_value={"status": "error", "message": "Chroma connection timeout"},
            ),
        ):
            res = client.post("/mcp/search", json={"query": "test"})
            assert res.status_code == 500
            assert "Chroma connection timeout" in res.json()["detail"]

    def test_toc_core_failure_returns_500(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_document_toc_core",
                return_value={"status": "error", "message": "Corrupt TOC metadata"},
            ),
        ):
            res = client.get("/mcp/documents/ITEM123/toc")
            assert res.status_code == 500
            assert "Corrupt TOC metadata" in res.json()["detail"]

    def test_read_core_failure_returns_500(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.read_document_core",
                return_value={"status": "error", "message": "Disk I/O error"},
            ),
        ):
            res = client.post("/mcp/documents/ITEM123/read", json={})
            assert res.status_code == 500
            assert "Disk I/O error" in res.json()["detail"]

    def test_relations_core_failure_returns_500(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.get_related_items_core",
                return_value={"status": "error", "message": "SQLite table locked"},
            ),
        ):
            res = client.get("/mcp/documents/ITEM123/relations")
            assert res.status_code == 500
            assert "SQLite table locked" in res.json()["detail"]


class TestMCPEdgeCases:
    """Verify edge cases including broad search scopes, unresolved assets, and DB locking."""

    @pytest.mark.parametrize("broad_key", ["all", "*", "library", "entire_library", None])
    def test_search_broad_item_key_succeeds(self, client, mock_active_syncer, broad_key):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.search_corpus_core",
                return_value={"status": "success", "results": []},
            ) as mock_search,
        ):
            payload = {"query": "test"}
            if broad_key is not None:
                payload["item_key"] = broad_key
            res = client.post("/mcp/search", json=payload)
            assert res.status_code == 200
            mock_search.assert_called_once()

    def test_read_unresolved_asset_ids_returns_404(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.read_document_core",
                return_value={
                    "status": "error",
                    "item_key": "ITEM123",
                    "assets": [],
                    "unresolved_asset_ids": ["f99"],
                },
            ),
        ):
            res = client.post("/mcp/documents/ITEM123/read", json={"asset_id": "f99"})
            assert res.status_code == 404
            assert "f99" in res.json()["detail"]

    def test_read_omitted_json_body_succeeds(self, client, mock_active_syncer):
        with (
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_active_syncer),
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch(
                "zocrates.fast_api_app.read_document_core",
                return_value={"status": "success", "item_key": "ITEM123", "assets": []},
            ) as mock_read,
        ):
            res = client.post("/mcp/documents/ITEM123/read")
            assert res.status_code == 200
            mock_read.assert_called_once_with(
                item_key="ITEM123",
                asset_id=None,
                page=None,
            )

    def test_open_meta_db_concurrent_initialization(self, tmp_path):
        """Verify _open_meta_db double-checked locking handles concurrent threads safely."""
        import threading
        from zocrates.rag.document_processor import _INITIALIZED_META_DBS, _open_meta_db

        cache_dir = tmp_path / "concurrent_cache"
        assert cache_dir not in _INITIALIZED_META_DBS

        errors = []

        def worker():
            try:
                conn = _open_meta_db(cache_dir)
                conn.execute("SELECT 1 FROM conversion_meta LIMIT 1;").fetchall()
                conn.close()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors encountered during concurrent initialization: {errors}"
        assert cache_dir in _INITIALIZED_META_DBS

