"""Unit tests for loopback API authentication and CORS hardening (Task 1)."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

import zocrates.config as config_mod
from zocrates.config import ALLOW_ORIGINS, DEFAULT_ALLOW_ORIGINS, get_auth_token
from zocrates.fast_api_app import app
from zocrates.mcp.backend_client import BackendClient


class TestAuthTokenConfigAndCORS:
    """Verify get_auth_token resolution and ALLOW_ORIGINS defaults in config.py."""

    def test_allow_origins_never_defaults_to_wildcard(self):
        assert "*" not in DEFAULT_ALLOW_ORIGINS
        assert "chrome://zocrates" in DEFAULT_ALLOW_ORIGINS
        assert "moz-extension://*" in DEFAULT_ALLOW_ORIGINS
        assert "*" not in ALLOW_ORIGINS

    def test_get_auth_token_prefers_env_var(self, monkeypatch):
        monkeypatch.setenv("ZOCRATES_AUTH_TOKEN", "custom-env-token-123")
        assert get_auth_token() == "custom-env-token-123"

    def test_get_auth_token_reads_and_generates_sidecar_with_0600_mode(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("ZOCRATES_AUTH_TOKEN", raising=False)
        old_cached = config_mod._CACHED_AUTH_TOKEN
        try:
            config_mod._CACHED_AUTH_TOKEN = None
            generated = get_auth_token(tmp_path)
            assert len(generated) == 64
            int(generated, 16)  # valid hex

            sidecar = tmp_path / "zocrates-data" / "backend.auth"
            assert sidecar.is_file()
            assert sidecar.read_text(encoding="utf-8").strip() == generated
            if os.name != "nt":
                mode = stat.S_IMODE(sidecar.stat().st_mode)
                assert mode == 0o600

            # Subsequent call reads the persisted sidecar token
            config_mod._CACHED_AUTH_TOKEN = None
            assert get_auth_token(tmp_path) == generated
        finally:
            config_mod._CACHED_AUTH_TOKEN = old_cached


class TestFastAPIAuthEnforcement:
    """Verify protected endpoints reject unauthenticated requests and accept valid tokens."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/chat"),
            ("POST", "/chat/resume"),
            ("GET", "/sessions"),
            ("POST", "/sessions"),
            ("GET", "/sessions/storage_stats"),
            ("POST", "/sessions/maintenance"),
            ("GET", "/sessions/sess-1"),
            ("DELETE", "/sessions/sess-1"),
            ("POST", "/sessions/sess-1/suggest_next_prompt"),
            ("POST", "/sessions/sess-1/rewind"),
            ("GET", "/config"),
            ("POST", "/config"),
            ("GET", "/llm/openrouter_models"),
            ("POST", "/llm/test"),
            ("POST", "/llm/test_embedding"),
            ("POST", "/llm/test_decision_model"),
            ("GET", "/llm/decision_tool/status"),
            ("POST", "/llm/decision_tool/install"),
            ("POST", "/llm/decision_tool/start"),
            ("POST", "/llm/decision_tool/stop"),
            ("GET", "/rag/collections"),
            ("POST", "/rag/reset"),
            ("POST", "/rag/model"),
            ("POST", "/rag/converter"),
            ("POST", "/mcp/search"),
            ("GET", "/mcp/documents/ITEM1234/toc"),
            ("POST", "/mcp/documents/ITEM1234/read"),
            ("GET", "/mcp/documents/ITEM1234/relations"),
            ("GET", "/mcp/catalog"),
            ("POST", "/discover/search"),
            ("POST", "/discover/resolve"),
            ("POST", "/discover/prepare_batch"),
            ("POST", "/discover/cleanup_batch"),
            ("POST", "/discovery/test_openalex"),
            ("POST", "/register"),
            ("GET", "/converters"),
            ("POST", "/converters/test-connection"),
            ("GET", "/items/ITEM1234/status"),
            ("GET", "/library/conversion-status"),
            ("POST", "/convert/ITEM1234"),
            ("GET", "/cost-estimate"),
            ("POST", "/batch-convert"),
            ("POST", "/batch-index"),
            ("POST", "/sync"),
            ("POST", "/sync/stream"),
            ("POST", "/sync/abort"),
            ("POST", "/items/delete"),
            ("GET", "/assets/ITEM1234/images/fig1.png"),
            ("POST", "/shutdown"),
            ("GET", "/graph/data"),
            ("GET", "/graph/view"),
            ("POST", "/graph/recompute"),
        ],
    )
    def test_protected_routes_return_401_without_token(self, method: str, path: str):
        unauth_client = TestClient(app)
        res = unauth_client.request(method, path)
        assert res.status_code == 401
        assert res.json()["detail"] == "Unauthorized"

    def test_invalid_bearer_token_returns_401(self):
        bad_client = TestClient(app, headers={"Authorization": "Bearer wrong-token-value"})
        res = bad_client.get("/config")
        assert res.status_code == 401

    def test_valid_bearer_and_custom_header_succeed(self):
        token = get_auth_token()
        bearer_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res_bearer = bearer_client.get("/config")
        assert res_bearer.status_code == 200

        header_client = TestClient(app, headers={"X-Zocrates-Auth": token})
        res_header = header_client.get("/config")
        assert res_header.status_code == 200

    def test_query_token_accepted_only_for_assets_and_graph_view(self, tmp_path: Path):
        token = get_auth_token()
        unauth_client = TestClient(app)

        # Non-asset route rejects query param token
        res_llm = unauth_client.get(f"/config?token={token}")
        assert res_llm.status_code == 401

        # GET /graph/view accepts query param token
        with patch("zocrates.fast_api_app.GRAPH_VIEW_TEMPLATE_PATH", tmp_path / "graph.html"):
            (tmp_path / "graph.html").write_text("<html>Graph</html>")
            res_graph = unauth_client.get(f"/graph/view?token={token}")
            assert res_graph.status_code == 200

        # GET /assets accepts query param token
        mock_syncer = MagicMock()
        cache_base = tmp_path / "zocrates-data" / "corpus"
        mock_syncer._zotero_data_dir = tmp_path
        mock_syncer.processor._cache_dir = cache_base
        asset_file = cache_base / "ITEM1234" / "images" / "fig.png"
        asset_file.parent.mkdir(parents=True, exist_ok=True)
        asset_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res_asset = unauth_client.get(f"/assets/ITEM1234/images/fig.png?token={token}")
            assert res_asset.status_code == 200

    def test_log_requests_redacts_token_in_query_string(self, caplog):
        import logging
        token = get_auth_token()
        unauth_client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="zocrates.fast_api_app"):
            unauth_client.get(f"/status?token={token}&mode=test")

        log_text = caplog.text
        assert token not in log_text
        assert "token=[REDACTED]" in log_text

    def test_status_unauthenticated_returns_minimal_probe_payload(self):
        unauth_client = TestClient(app)
        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/mock_zotero_dir")
        mock_syncer.processor.list_cached_keys.return_value = ["SECRETKEY1", "SECRETKEY2"]

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = unauth_client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert set(data.keys()) == {"status", "zotero_data_dir", "version"}
            assert data["status"] == "ready"
            assert data["zotero_data_dir"] == "/tmp/mock_zotero_dir"
            assert "project_dir" not in data
            assert "cached_document_keys" not in data
            assert "session_storage_stats" not in data
            assert "user_research_profile" not in data
            assert "scholar_polite_email" not in data

    def test_status_authenticated_returns_full_payload(self):
        auth_client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})
        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/mock_zotero_dir")
        mock_syncer.processor.list_cached_keys.return_value = ["KEY1"]
        mock_syncer.store._embedder = MagicMock(dimension=768, _model_name="jinaai/jina-embeddings-v5-text-nano")
        mock_syncer.store.active_collection_name = "col1"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = auth_client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ready"
            assert data["cached_document_keys"] == ["KEY1"]
            assert "hardware" in data
            assert "embedding_info" in data

    def test_cors_rejects_external_browser_origin_and_allows_chrome_origin(self):
        unauth_client = TestClient(app)
        res_evil = unauth_client.options(
            "/chat",
            headers={
                "Origin": "https://malicious.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in res_evil.headers

        res_chrome = unauth_client.options(
            "/chat",
            headers={
                "Origin": "chrome://zocrates",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert res_chrome.headers.get("access-control-allow-origin") == "chrome://zocrates"

    def test_mcp_backend_client_authenticates_against_app(self):
        token = get_auth_token()
        test_client = TestClient(app)
        mcp_client = BackendClient(base_url="http://testserver", client=test_client, auth_token=token)
        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = "/tmp/mock_zotero"

        with (
            patch("zocrates.fast_api_app.is_mcp_server_enabled", return_value=True),
            patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer),
            patch("zocrates.fast_api_app.search_corpus_core", return_value={"status": "success", "results": []}),
        ):
            res = mcp_client.search("test query")
            assert res["status"] == "success"
