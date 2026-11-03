"""Unit tests for Phase 3: FastMCP Stdio Bridge Server and BackendClient.

Covers:
- BackendClient methods (search, get_toc, read_document, get_relations, get_catalog).
- BackendClient error handling (ConnectError, 403, 503, 404, TimeoutException, unexpected exceptions).
- BackendClient URL configuration, trailing slash stripping, and context manager.
- FastMCP tool definitions, docstrings, and execution via mock client.
- FastMCP resource endpoints (library index, document markdown, relations table).
- FastMCP literature_review prompt template.
- CLI entrypoint main() shutdown handling (KeyboardInterrupt, BrokenPipeError).
- Stdout purity verification (logging routes exclusively to stderr).
"""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zocrates.mcp.backend_client import BackendClient
from zocrates.mcp.server import (
    backend_client,
    get_document_markdown,
    get_document_relations,
    get_document_toc,
    get_library_index,
    get_related_items,
    literature_review,
    mcp,
    read_document,
    search_corpus,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.Client for testing BackendClient without real network calls."""
    client = MagicMock(spec=httpx.Client)
    return client


@pytest.fixture
def bclient(mock_httpx_client):
    """BackendClient instantiated with a mocked httpx.Client and explicit auth token."""
    return BackendClient(
        base_url="http://127.0.0.1:8765",
        client=mock_httpx_client,
        auth_token="test-mcp-token",
    )


# ===========================================================================
# 1. BackendClient Method & Request Tests
# ===========================================================================


class TestBackendClientMethods:
    """Verify BackendClient requests and parameter serializations."""

    def test_search_minimal_params(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "results": []}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.search(query="graph neural networks")

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "POST",
            "http://127.0.0.1:8765/mcp/search",
            json={"query": "graph neural networks", "limit": 10},
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_search_full_params(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "results": [{"chunk_id": "c1"}]}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.search(
            query="transformers",
            item_key=["ITEM1", "ITEM2"],
            chunk_type="frontmatter",
            limit=5,
            exclude_chunk_ids=["ex1", "ex2"],
        )

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "POST",
            "http://127.0.0.1:8765/mcp/search",
            json={
                "query": "transformers",
                "limit": 5,
                "item_key": ["ITEM1", "ITEM2"],
                "chunk_type": "frontmatter",
                "exclude_chunk_ids": ["ex1", "ex2"],
            },
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_get_toc(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "item_key": "ITEM123", "toc_content": "# TOC"}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.get_toc("ITEM123")

        assert res["status"] == "success"
        assert res["item_key"] == "ITEM123"
        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "http://127.0.0.1:8765/mcp/documents/ITEM123/toc",
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_read_document_default(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "assets": [{"content": "# Full"}]}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.read_document("ITEM123")

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "POST",
            "http://127.0.0.1:8765/mcp/documents/ITEM123/read",
            json={},
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_read_document_with_asset_and_page(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "assets": [{"content": "Section 1"}]}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.read_document("ITEM123", asset_id=["s1", "f1"], page=2)

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "POST",
            "http://127.0.0.1:8765/mcp/documents/ITEM123/read",
            json={"asset_id": ["s1", "f1"], "page": 2},
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_get_relations_default_ceiling(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "total_count": 0, "related_items": []}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.get_relations("ITEM123")

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "http://127.0.0.1:8765/mcp/documents/ITEM123/relations",
            params={"ceiling": 25},
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_get_relations_custom_ceiling(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "total_count": 5, "related_items": []}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.get_relations("ITEM123", ceiling=40)

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "http://127.0.0.1:8765/mcp/documents/ITEM123/relations",
            params={"ceiling": 40},
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_get_catalog(self, bclient, mock_httpx_client):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "content": "# Catalog"}
        mock_httpx_client.request.return_value = mock_resp

        res = bclient.get_catalog()

        assert res["status"] == "success"
        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "http://127.0.0.1:8765/mcp/catalog",
            headers={"Authorization": "Bearer test-mcp-token"},
        )

    def test_auth_token_resolution_from_env_and_sidecar(self, tmp_path, mock_httpx_client):
        # 1. Env var ZOCRATES_AUTH_TOKEN
        with patch.dict("os.environ", {"ZOCRATES_AUTH_TOKEN": "env-secret-token"}, clear=False):
            c_env = BackendClient(client=mock_httpx_client)
            assert c_env._auth_token == "env-secret-token"

        # 2. Sidecar file <zotero-data-dir>/zocrates-data/backend.auth
        zocrates_dir = tmp_path / "zocrates-data"
        zocrates_dir.mkdir(parents=True)
        (zocrates_dir / "backend.auth").write_text("sidecar-secret-token\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"ZOTERO_DATA_DIR": str(tmp_path), "ZOCRATES_AUTH_TOKEN": ""},
            clear=False,
        ):
            c_sidecar = BackendClient(client=mock_httpx_client)
            assert c_sidecar._auth_token == "sidecar-secret-token"

    def test_context_manager_closes_client(self, mock_httpx_client):
        with BackendClient(client=mock_httpx_client) as client:
            assert client is not None
        mock_httpx_client.close.assert_called_once()

    def test_base_url_resolution(self):
        # 1. Trailing slash stripping
        c1 = BackendClient(base_url="http://localhost:8765/")
        assert c1.base_url == "http://localhost:8765"

        # 2. Env var fallback
        with patch.dict("os.environ", {"ZOCRATES_BACKEND_URL": "http://192.168.1.100:9999/"}):
            c2 = BackendClient()
            assert c2.base_url == "http://192.168.1.100:9999"

        # 3. Default fallback
        with patch.dict("os.environ", {}, clear=True):
            c3 = BackendClient()
            assert c3.base_url == "http://127.0.0.1:8765"

    def test_default_client_timeout_configuration(self):
        client = BackendClient()
        assert client._client.timeout.connect == 5.0
        assert client._client.timeout.read == 120.0
        assert client._client.timeout.write == 10.0
        assert client._client.timeout.pool == 5.0
        client.close()


# ===========================================================================
# 2. BackendClient Error Handling Tests
# ===========================================================================


class TestBackendClientErrorHandling:
    """Verify standard structured error mappings for network and status failures."""

    def test_connect_error_returns_offline_guidance(self, bclient, mock_httpx_client):
        req = httpx.Request("GET", "http://127.0.0.1:8765/mcp/catalog")
        mock_httpx_client.request.side_effect = httpx.ConnectError("Connection refused", request=req)

        res = bclient.get_catalog()

        assert res["status"] == "error"
        assert "Zocrates backend service is offline" in res["message"]
        assert "Please open Zotero with Zocrates enabled" in res["message"]

    def test_403_forbidden_returns_disabled_guidance(self, bclient, mock_httpx_client):
        req = httpx.Request("POST", "http://127.0.0.1:8765/mcp/search")
        resp = httpx.Response(403, request=req, json={"detail": "Forbidden"})
        mock_httpx_client.request.return_value = resp
        mock_httpx_client.request.side_effect = None
        # raise_for_status will trigger HTTPStatusError
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("403", request=req, response=resp))

        res = bclient.search("query")

        assert res["status"] == "error"
        assert "MCP server access is disabled in Zocrates preferences" in res["message"]

    def test_503_service_unavailable_returns_syncer_guidance(self, bclient, mock_httpx_client):
        req = httpx.Request("GET", "http://127.0.0.1:8765/mcp/documents/ITEM1/toc")
        resp = httpx.Response(503, request=req, json={"detail": "Not synced"})
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("503", request=req, response=resp))
        mock_httpx_client.request.return_value = resp

        res = bclient.get_toc("ITEM1")

        assert res["status"] == "error"
        assert "Zotero library is not yet registered or synced" in res["message"]

    def test_404_not_found_extracts_detail(self, bclient, mock_httpx_client):
        req = httpx.Request("GET", "http://127.0.0.1:8765/mcp/documents/NONEXIST/toc")
        resp = httpx.Response(404, request=req, json={"detail": "Document bundle for NONEXIST not found."})
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("404", request=req, response=resp))
        mock_httpx_client.request.return_value = resp

        res = bclient.get_toc("NONEXIST")

        assert res["status"] == "error"
        assert res["message"] == "Document bundle for NONEXIST not found."

    def test_400_bad_request_extracts_message_or_detail(self, bclient, mock_httpx_client):
        req = httpx.Request("POST", "http://127.0.0.1:8765/mcp/search")
        resp = httpx.Response(400, request=req, json={"message": "Invalid chunk type"})
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("400", request=req, response=resp))
        mock_httpx_client.request.return_value = resp

        res = bclient.search("query")

        assert res["status"] == "error"
        assert res["message"] == "Invalid chunk type"

    def test_non_json_http_error_falls_back_to_text(self, bclient, mock_httpx_client):
        req = httpx.Request("GET", "http://127.0.0.1:8765/mcp/catalog")
        resp = httpx.Response(500, request=req, text="Fatal Internal Server Error")
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("500", request=req, response=resp))
        mock_httpx_client.request.return_value = resp

        res = bclient.get_catalog()

        assert res["status"] == "error"
        assert res["message"] == "Fatal Internal Server Error"

    def test_timeout_exception_returns_busy_guidance(self, bclient, mock_httpx_client):
        req = httpx.Request("POST", "http://127.0.0.1:8765/mcp/search")
        mock_httpx_client.request.side_effect = httpx.ReadTimeout("Read timed out", request=req)

        res = bclient.search("heavy retrieval query")

        assert res["status"] == "error"
        assert "Zocrates backend timed out while processing retrieval" in res["message"]
        assert "The server may be busy with a batch-indexing or conversion task" in res["message"]

    def test_unexpected_exception_returns_generic_error(self, bclient, mock_httpx_client):
        mock_httpx_client.request.side_effect = ValueError("DNS resolution failed abruptly")

        res = bclient.get_catalog()

        assert res["status"] == "error"
        assert "Unexpected backend communication error" in res["message"]
        assert "DNS resolution failed abruptly" in res["message"]


# ===========================================================================
# 3. FastMCP Tools Unit Tests
# ===========================================================================


class TestFastMCPTools:
    """Verify FastMCP tools call BackendClient correctly."""

    def test_search_corpus_tool(self):
        with patch.object(backend_client, "search", return_value={"status": "success", "results": []}) as mock_search:
            res = search_corpus(query="neural networks", item_key="KEY1", chunk_type="text", limit=15)
            assert res == {"status": "success", "results": []}
            mock_search.assert_called_once_with(
                query="neural networks",
                item_key="KEY1",
                chunk_type="text",
                limit=15,
            )

    def test_get_document_toc_tool(self):
        with patch.object(backend_client, "get_toc", return_value={"status": "success", "toc_content": "# TOC"}) as mock_toc:
            res = get_document_toc("DOC123")
            assert res == {"status": "success", "toc_content": "# TOC"}
            mock_toc.assert_called_once_with(item_key="DOC123")

    def test_read_document_tool(self):
        with patch.object(backend_client, "read_document", return_value={"status": "success", "assets": []}) as mock_read:
            res = read_document(item_key="DOC123", asset_id=["s1", "t1"], page=2)
            assert res == {"status": "success", "assets": []}
            mock_read.assert_called_once_with(item_key="DOC123", asset_id=["s1", "t1"], page=2)

    def test_get_related_items_tool(self):
        with patch.object(backend_client, "get_relations", return_value={"status": "success", "total_count": 0}) as mock_rel:
            res = get_related_items(item_key="DOC123", ceiling=30)
            assert res == {"status": "success", "total_count": 0}
            mock_rel.assert_called_once_with(item_key="DOC123", ceiling=30)

    @pytest.mark.asyncio
    async def test_tool_execution_via_fastmcp_call_tool(self):
        """Verify tool execution through the FastMCP router."""
        with patch.object(backend_client, "search", return_value={"status": "success", "results": [{"title": "Paper"}]}):
            call_res = await mcp.call_tool("search_corpus", {"query": "transformer"})
            assert call_res is not None
            # FastMCP call_tool returns (content_list, raw_result)
            content_list, raw_dict = call_res
            assert len(content_list) > 0
            text_content = content_list[0].text
            parsed = json.loads(text_content)
            assert parsed["status"] == "success"
            assert parsed["results"][0]["title"] == "Paper"
            assert raw_dict["status"] == "success"


# ===========================================================================
# 4. FastMCP Resources Tests
# ===========================================================================


class TestFastMCPResources:
    """Verify FastMCP resource endpoints and markdown formatting."""

    def test_library_index_resource_success(self):
        with patch.object(backend_client, "get_catalog", return_value={"status": "success", "content": "# Index Content"}):
            content = get_library_index()
            assert content == "# Index Content"

    def test_library_index_resource_error(self):
        with patch.object(
            backend_client,
            "get_catalog",
            return_value={"status": "error", "message": "Backend offline"},
        ):
            content = get_library_index()
            assert content == "Error retrieving library index: Backend offline"

    def test_document_markdown_resource_success(self):
        with patch.object(
            backend_client,
            "read_document",
            return_value={
                "status": "success",
                "assets": [
                    {"content": "# Section 1\nText"},
                    {"content": "## Section 2\nMore text"},
                ],
            },
        ):
            content = get_document_markdown("ITEM123")
            assert "# Section 1\nText\n\n## Section 2\nMore text" == content

    def test_document_markdown_resource_empty_assets(self):
        with patch.object(
            backend_client,
            "read_document",
            return_value={"status": "success", "assets": []},
        ):
            content = get_document_markdown("ITEM123")
            assert "No readable document content found for ITEM123" in content
        with patch.object(
            backend_client,
            "read_document",
            return_value={"status": "error", "message": "Document not found"},
        ):
            content = get_document_markdown("ITEM123")
            assert content == "Error reading document: Document not found"

    def test_document_relations_resource_success_with_items(self):
        with patch.object(
            backend_client,
            "get_relations",
            return_value={
                "status": "success",
                "item_key": "ITEM123",
                "total_count": 2,
                "related_items": [
                    {
                        "item_key": "REL1",
                        "title": "Paper 1 | Special",
                        "relation_type": "hybrid_search",
                        "score": 0.8912,
                    },
                    {
                        "item_key": "REL2",
                        "title": "Paper 2",
                        "relation_type": "shared_tags",
                        "score": None,
                    },
                ],
            },
        ):
            table = get_document_relations("ITEM123")
            assert "# Related Documents for ITEM123" in table
            assert "Total Relations: 2" in table
            assert "| REL1 | Paper 1 \\| Special | hybrid_search | 0.891 |" in table
            assert "| REL2 | Paper 2 | shared_tags | N/A |" in table

    def test_document_relations_resource_empty(self):
        with patch.object(
            backend_client,
            "get_relations",
            return_value={
                "status": "success",
                "item_key": "ITEM123",
                "total_count": 0,
                "related_items": [],
            },
        ):
            msg = get_document_relations("ITEM123")
            assert "No verified relations found in the knowledge graph" in msg

    def test_document_relations_resource_error(self):
        with patch.object(
            backend_client,
            "get_relations",
            return_value={"status": "error", "message": "Database locked"},
        ):
            msg = get_document_relations("ITEM123")
            assert msg == "Error retrieving relations: Database locked"

    @pytest.mark.asyncio
    async def test_resource_read_via_fastmcp(self):
        """Verify resource reading through the FastMCP router."""
        with patch.object(backend_client, "get_catalog", return_value={"status": "success", "content": "# Index MD"}):
            res_content = await mcp.read_resource("zotero://library/index")
            assert res_content is not None
            assert len(res_content) > 0
            assert res_content[0].content == "# Index MD"

        with patch.object(
            backend_client,
            "read_document",
            return_value={"status": "success", "assets": [{"content": "# Doc MD"}]},
        ):
            res_doc = await mcp.read_resource("zotero://items/ITEM123/document.md")
            assert res_doc[0].content == "# Doc MD"


# ===========================================================================
# 5. FastMCP Prompts Tests
# ===========================================================================


class TestFastMCPPrompts:
    """Verify literature_review prompt template construction."""

    def test_literature_review_prompt_direct(self):
        text = literature_review("Reinforcement Learning from Human Feedback", max_papers=6)
        assert 'Reinforcement Learning from Human Feedback' in text
        assert 'search_corpus(query="Reinforcement Learning from Human Feedback", limit=12)' in text
        assert 'up to 6' in text
        assert 'Step 1: Broad & Targeted Retrieval' in text
        assert 'Step 2: Metadata & Structure Triage' in text
        assert 'Step 3: Targeted Reading & Evidence Extraction' in text
        assert 'Step 4: Grounded Academic Synthesis' in text

    @pytest.mark.asyncio
    async def test_literature_review_via_fastmcp_get_prompt(self):
        prompt_res = await mcp.get_prompt("literature_review", {"topic": "Quantum Computing", "max_papers": 3})
        assert prompt_res is not None
        assert len(prompt_res.messages) == 1
        msg = prompt_res.messages[0]
        assert msg.role == "user"
        content_text = msg.content.text
        assert "Quantum Computing" in content_text
        assert "up to 3" in content_text


# ===========================================================================
# 6. CLI Entrypoint & Stdout Purity Tests
# ===========================================================================


class TestCLIEntrypointAndPurity:
    """Verify clean shutdown and strict logging to sys.stderr."""

    def test_main_keyboard_interrupt_exits_cleanly(self):
        from zocrates.mcp.__main__ import main

        with (
            patch.object(mcp, "run", side_effect=KeyboardInterrupt),
            patch("sys.exit") as mock_exit,
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            main()
            mock_exit.assert_called_once_with(0)
            assert "Zocrates MCP server shut down cleanly." in mock_stderr.getvalue()

    def test_main_broken_pipe_exits_cleanly(self):
        from zocrates.mcp.__main__ import main

        with (
            patch.object(mcp, "run", side_effect=BrokenPipeError),
            patch("sys.exit") as mock_exit,
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            main()
            mock_exit.assert_called_once_with(0)
            assert "Zocrates MCP server shut down cleanly." in mock_stderr.getvalue()

    def test_backend_client_logging_goes_to_stderr_never_stdout(self, capsys):
        """Assert that logger calls in BackendClient write exclusively to stderr."""
        req = httpx.Request("GET", "http://127.0.0.1:8765/mcp/catalog")
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = httpx.ConnectError("Offline", request=req)

        client = BackendClient(client=mock_client)
        client.get_catalog()

        captured = capsys.readouterr()
        # stdout MUST be completely pure
        assert captured.out == ""
        # stderr must have captured the connection error log
        assert "Failed to connect to Zocrates backend" in captured.err

    def test_stdio_subprocess_lifecycle_and_stdout_purity(self):
        """Spawn 'python -m zocrates.mcp' and assert stdout contains 100% valid JSON-RPC frames."""
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, "-m", "zocrates.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()

            init_line = proc.stdout.readline()
            assert init_line.strip() != ""
            init_resp = json.loads(init_line)
            assert init_resp["jsonrpc"] == "2.0"
            assert init_resp["id"] == 1
            assert init_resp["result"]["serverInfo"]["name"] == "zocrates-library"

            # Initialized notification
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            proc.stdin.write(json.dumps(notif) + "\n")
            proc.stdin.flush()

            # Tools list request
            tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            proc.stdin.write(json.dumps(tools_req) + "\n")
            proc.stdin.flush()

            tools_line = proc.stdout.readline()
            tools_resp = json.loads(tools_line)
            assert tools_resp["id"] == 2
            tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
            assert "search_corpus" in tool_names
            assert "get_document_toc" in tool_names
            assert "read_document" in tool_names
            assert "get_related_items" in tool_names
        finally:
            proc.terminate()
            proc.wait(timeout=3)
