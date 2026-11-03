"""Unit tests for Phase 6 REST API endpoints.

Tests:
  - GET /converters lists registered converters and active converter status.
  - POST /convert/{item_key} re-converts item with specified/active converter and re-indexes.
  - GET /cost-estimate pre-scans PDF page counts and calculates estimated cost.
"""

import asyncio
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app


def parse_sse_events(raw_text: str) -> list[dict]:
    """Helper to parse raw SSE stream text into structured event dictionaries."""
    events = []
    current_event = {}
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            if current_event:
                events.append(current_event)
                current_event = {}
            continue
        if line.startswith("event:"):
            current_event["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            raw_data = line[len("data:") :].strip()
            try:
                current_event["data"] = json.loads(raw_data)
            except Exception:
                current_event["data"] = raw_data
    if current_event:
        events.append(current_event)
    return events


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    """Reset sse_starlette AppStatus should_exit_event before each test to prevent event loop mismatch."""
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


class TestConvertersEndpoint:
    """Verify GET /converters endpoint."""

    def test_converters_endpoint_unregistered(self, client) -> None:
        """When syncer is not registered, lists default converters."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/converters")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "converters" in data
            converter_ids = [c["id"] for c in data["converters"]]
            assert "direct_read" in converter_ids
            assert "markitdown" in converter_ids

    def test_converters_endpoint_registered(self, client) -> None:
        """When syncer is registered, returns processor converters."""
        mock_syncer = MagicMock()
        mock_conv1 = MagicMock()
        mock_conv1.converter_id = "direct_read"
        mock_conv1.converter_version = "1.0.0"

        mock_conv2 = MagicMock()
        mock_conv2.converter_id = "markitdown"
        mock_conv2.converter_version = "1.0.0"

        mock_syncer.processor._converters = [mock_conv1, mock_conv2]
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/converters")
            assert res.status_code == 200
            data = res.json()
            assert data["document_converter_id"] == "markitdown"
            assert len(data["converters"]) == 2


class TestConverterTestConnectionEndpoint:
    """Verify POST /converters/test-connection endpoint."""

    def test_missing_datalab_api_key_returns_error(self, client) -> None:
        """When no key is provided and none in environment, returns missing key error."""
        with patch("zocrates.config.get_datalab_api_key", return_value=None):
            res = client.post("/converters/test-connection", json={"converter_id": "datalab"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "Datalab API key is missing" in data["message"]

    def test_datalab_connection_success(self, client) -> None:
        """Valid Datalab credentials successfully return connected status."""
        mock_client = MagicMock()
        mock_client.list_files.return_value = []
        with patch("datalab_sdk.DatalabClient", return_value=mock_client):
            res = client.post(
                "/converters/test-connection",
                json={"converter_id": "datalab", "datalab_api_key": "valid-key-123"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "Connected to Datalab API successfully" in data["message"]

    def test_datalab_authentication_error_exception(self, client) -> None:
        """DatalabAuthenticationError is cleanly detected and reported as Unauthorized error."""
        from datalab_sdk import DatalabAuthenticationError

        mock_client = MagicMock()
        mock_client.list_files.side_effect = DatalabAuthenticationError("Invalid API key or access token")
        with patch("datalab_sdk.DatalabClient", return_value=mock_client):
            res = client.post(
                "/converters/test-connection",
                json={"converter_id": "datalab", "datalab_api_key": "bad-key-auth-err"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "Datalab API key rejected (Unauthorized)" in data["message"]
            assert "Invalid API key or access token" in data["message"]

    def test_datalab_invalid_api_key_string_error(self, client) -> None:
        """Generic exception mentioning 'invalid api key' is detected as Unauthorized."""
        mock_client = MagicMock()
        mock_client.list_files.side_effect = RuntimeError("Failed with invalid API key in header")
        with patch("datalab_sdk.DatalabClient", return_value=mock_client):
            res = client.post(
                "/converters/test-connection",
                json={"converter_id": "datalab", "datalab_api_key": "bad-key-str"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "Datalab API key rejected (Unauthorized)" in data["message"]

    def test_datalab_unauthorized_401_error(self, client) -> None:
        """HTTP 401 / Unauthorized is detected as Unauthorized error."""
        mock_client = MagicMock()
        mock_client.list_files.side_effect = Exception("Client error '401 Unauthorized'")
        with patch("datalab_sdk.DatalabClient", return_value=mock_client):
            res = client.post(
                "/converters/test-connection",
                json={"converter_id": "datalab", "datalab_api_key": "bad-key-401"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "Datalab API key rejected (Unauthorized)" in data["message"]

    def test_datalab_offline_temporary_network_notice(self, client) -> None:
        """Non-auth network error indicates SDK initialized with probe notice."""
        mock_client = MagicMock()
        mock_client.list_files.side_effect = ConnectionError("DNS resolution failed")
        with patch("datalab_sdk.DatalabClient", return_value=mock_client):
            res = client.post(
                "/converters/test-connection",
                json={"converter_id": "datalab", "datalab_api_key": "key-offline"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "Datalab SDK initialized with provided key" in data["message"]
            assert "DNS resolution failed" in data["message"]

    def test_local_converters_test_connection(self, client) -> None:
        """Local converters return immediately available."""
        for conv_id in ("markitdown", "direct_read", "docling_local"):
            res = client.post("/converters/test-connection", json={"converter_id": conv_id})
            assert res.status_code == 200
            assert res.json()["status"] == "success"
            assert f"Converter '{conv_id}' is available locally." in res.json()["message"]

    def test_unknown_converter_test_connection(self, client) -> None:
        """Unknown converter returns error."""
        res = client.post("/converters/test-connection", json={"converter_id": "nonexistent"})
        assert res.status_code == 200
        assert res.json()["status"] == "error"
        assert "Unknown converter ID" in res.json()["message"]


class TestConvertItemEndpoint:
    """Verify POST /convert/{item_key} endpoint."""

    def test_convert_item_unregistered_returns_400(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/convert/KEY123")
            assert res.status_code == 400
            assert "not registered" in res.json()["detail"].lower()

    def test_convert_item_missing_attachment_returns_404(self, client) -> None:
        mock_syncer = MagicMock()
        mock_syncer.db.get_attachment_by_key.return_value = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/NONEXISTENT")
            assert res.status_code == 404
            assert "No valid attachment" in res.json()["detail"]

    def test_convert_item_success(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()

        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "ATTACH_KEY1"
        mock_att.parent_item_key = None
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_type = "journalArticle"
        mock_parent.title = "Sample Paper Title"
        mock_parent.collections = []

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = mock_parent
        mock_syncer.processor.convert_with.return_value = (
            "---\ntype: journalArticle\n---\n# Sample Paper Title\n\nContent body"
        )
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/ATTACH_KEY1", json={"converter_id": "markitdown"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ATTACH_KEY1"
            assert data["converter_id"] == "markitdown"
            assert data["char_count"] > 0
            assert data["chunk_count"] > 0

            # Verify immediate re-indexing call
            mock_syncer.store.ingest_document.assert_called_once()
            mock_syncer.processor.update_indexed_at.assert_called_once_with("ATTACH_KEY1")

    def test_convert_item_skipped_when_already_converted_force_false(self, client, tmp_path) -> None:
        from zocrates.rag.document_processor import _file_sha256

        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")
        current_hash = _file_sha256(dummy_pdf)

        mock_att = MagicMock()
        mock_att.item_key = "ATTACH_KEY1"
        mock_att.parent_item_key = "PARENT_KEY1"
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_type = "journalArticle"
        mock_parent.title = "Sample Paper Title"
        mock_parent.collections = []

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = mock_parent
        mock_syncer.processor.is_cached.return_value = True
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "markitdown",
            "source_hash": current_hash,
        }
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/ATTACH_KEY1", json={"converter_id": "markitdown"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "skipped"
            assert data["already_converted"] is True
            assert data["item_key"] == "ATTACH_KEY1"
            assert data["parent_item_key"] == "PARENT_KEY1"
            assert data["converter_id"] == "markitdown"
            assert "Already converted" in data["message"]

            mock_syncer.processor.convert_with.assert_not_called()
            mock_syncer.store.ingest_document.assert_not_called()

    def test_convert_item_reconverted_when_hash_mismatched_force_false(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "ATTACH_KEY1"
        mock_att.parent_item_key = "PARENT_KEY1"
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_type = "journalArticle"
        mock_parent.title = "Sample Paper Title"
        mock_parent.collections = []

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = mock_parent
        mock_syncer.processor.is_cached.return_value = True
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "markitdown",
            "source_hash": "old_outdated_hash_12345",
        }
        mock_syncer.processor.convert_with.return_value = (
            "---\ntype: journalArticle\n---\n# Sample Paper Title\n\nContent body"
        )
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/ATTACH_KEY1", json={"converter_id": "markitdown", "force": False})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ATTACH_KEY1"
            assert data["converter_id"] == "markitdown"

            mock_syncer.processor.convert_with.assert_called_once()
            mock_syncer.store.ingest_document.assert_called_once()
            mock_syncer.processor.update_indexed_at.assert_called_once_with("ATTACH_KEY1")

    def test_convert_item_reconverted_when_not_cached_on_disk_force_false(self, client, tmp_path) -> None:
        from zocrates.rag.document_processor import _file_sha256

        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")
        current_hash = _file_sha256(dummy_pdf)

        mock_att = MagicMock()
        mock_att.item_key = "ATTACH_KEY1"
        mock_att.parent_item_key = "PARENT_KEY1"
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_type = "journalArticle"
        mock_parent.title = "Sample Paper Title"
        mock_parent.collections = []

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = mock_parent
        mock_syncer.processor.is_cached.return_value = False
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "markitdown",
            "source_hash": current_hash,
        }
        mock_syncer.processor.convert_with.return_value = (
            "---\ntype: journalArticle\n---\n# Sample Paper Title\n\nContent body"
        )
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/ATTACH_KEY1", json={"converter_id": "markitdown", "force": False})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ATTACH_KEY1"

            mock_syncer.processor.convert_with.assert_called_once()
            mock_syncer.store.ingest_document.assert_called_once()
            mock_syncer.processor.update_indexed_at.assert_called_once_with("ATTACH_KEY1")

    def test_convert_item_reconverted_when_force_true(self, client, tmp_path) -> None:
        from zocrates.rag.document_processor import _file_sha256

        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")
        current_hash = _file_sha256(dummy_pdf)

        mock_att = MagicMock()
        mock_att.item_key = "ATTACH_KEY1"
        mock_att.parent_item_key = "PARENT_KEY1"
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_type = "journalArticle"
        mock_parent.title = "Sample Paper Title"
        mock_parent.collections = []

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = mock_parent
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "markitdown",
            "source_hash": current_hash,
        }
        mock_syncer.processor.convert_with.return_value = (
            "---\ntype: journalArticle\n---\n# Sample Paper Title\n\nContent body"
        )
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/ATTACH_KEY1", json={"converter_id": "markitdown", "force": True})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ATTACH_KEY1"
            assert data["converter_id"] == "markitdown"
            assert data["char_count"] > 0
            assert data["chunk_count"] > 0

            mock_syncer.processor.convert_with.assert_called_once()
            mock_syncer.store.ingest_document.assert_called_once()
            mock_syncer.processor.update_indexed_at.assert_called_once_with("ATTACH_KEY1")


    def test_convert_item_datalab_non_pdf_resolves_to_fallback(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_html = tmp_path / "web.html"
        dummy_html.write_text("<html>hello</html>", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "HTML_KEY"
        mock_att.parent_item_key = None
        mock_att.item_id = 43
        mock_att.parent_item_id = None
        mock_att.content_type = "text/html"
        mock_att.path = dummy_html

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = None
        mock_syncer.processor.datalab_non_pdf_fallback = "markitdown"
        mock_syncer.processor.convert_with.return_value = (
            "---\ntype: webpage\n---\n# Webpage Title\n\nContent body"
        )

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/HTML_KEY", json={"converter_id": "datalab"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["converter_id"] == "markitdown"
            # Verify convert_with was called with effective converter
            mock_syncer.processor.convert_with.assert_called_once()
            call_kwargs = mock_syncer.processor.convert_with.call_args[1]
            assert call_kwargs["converter_id"] == "markitdown"

    def test_convert_item_failure_returns_structured_error(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "FAIL_KEY"
        mock_att.parent_item_key = None
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = None
        mock_syncer.processor.convert_with.side_effect = RuntimeError("CUDA OOM")

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/convert/FAIL_KEY", json={"converter_id": "docling_local"})
            assert res.status_code == 500
            data = res.json()
            assert "detail" in data
            assert data["detail"]["error"] == "conversion_failed"
            assert "CUDA OOM" in data["detail"]["message"]
            assert data["detail"]["converter_id"] == "docling_local"

    def test_convert_item_timeout_returns_504(self, client, tmp_path) -> None:
        import asyncio

        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf bytes", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "TIMEOUT_KEY"
        mock_att.parent_item_key = None
        mock_att.item_id = 42
        mock_att.parent_item_id = 10
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.db.get_item.return_value = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
                res = client.post("/convert/TIMEOUT_KEY", json={"converter_id": "docling_local"})
                assert res.status_code == 504
                data = res.json()
                assert "detail" in data
                assert data["detail"]["error"] == "conversion_timeout"
                mock_syncer.processor.record_failure.assert_called_once()


class TestCostEstimateEndpoint:
    """Verify GET /cost-estimate endpoint."""

    def test_cost_estimate_unregistered_returns_400(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/cost-estimate")
            assert res.status_code == 400

    def test_cost_estimate_targeted_keys(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "paper.pdf"
        dummy_pdf.write_text("pdf content", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "KEY1"
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.processor.get_metadata.return_value = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.rag.pdf_utils.count_pdf_pages", return_value=10):
                res = client.get("/cost-estimate?converter_id=gemini_vision&item_keys=KEY1,KEY2")
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "success"
                assert data["converter_id"] == "gemini_vision"
                assert data["target_documents"] == 2
                assert data["eligible_documents"] == 2
                assert data["already_converted_count"] == 0
                assert data["total_pages"] == 20
                assert data["cost_per_page_usd"] == 0.0005
                assert data["estimated_cost_usd"] == 0.01
                assert "datalab_merge_cross_page" in data
                assert "vlm_captioning" in data
                assert data["vlm_captioning"]["enabled"] is True
                assert data["vlm_captioning"]["provider"] == "openrouter"
                assert "model" in data["vlm_captioning"]
                assert "max_images_per_doc" in data["vlm_captioning"]
                # Phase 7 default alignment (§5.2): the default VLM model is
                # now the paid deepseek-v4.1-flash, so it is not free-tier.
                assert data["vlm_captioning"]["is_free_tier"] is False

    def test_cost_estimate_parent_item_resolution(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "parent_paper.pdf"
        dummy_pdf.write_text("pdf content", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "CHILD_ATT1"
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        mock_parent = MagicMock()
        mock_parent.item_id = 99
        mock_parent.title = "Parent Paper"

        mock_syncer.db.get_attachment_by_key.return_value = None
        mock_syncer.db.get_item_by_key.return_value = mock_parent
        mock_syncer.db.get_attachments.return_value = [mock_att]
        mock_syncer.processor.get_metadata.return_value = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.rag.pdf_utils.count_pdf_pages", return_value=5):
                res = client.get("/cost-estimate?converter_id=datalab&item_keys=PARENT_KEY")
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "success"
                assert data["target_documents"] == 1
                assert data["total_pages"] == 5
                assert data["cost_per_page_usd"] == 0.004
                assert data["estimated_cost_usd"] == 0.02

    def test_cost_estimate_datalab_includes_non_pdf_with_zero_cost(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_html = tmp_path / "snapshot.html"
        dummy_html.write_text("<html>body</html>", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "HTML_ATT"
        mock_att.content_type = "text/html"
        mock_att.path = dummy_html

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.processor.get_metadata.return_value = None
        mock_syncer.processor.datalab_non_pdf_fallback = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/cost-estimate?converter_id=datalab&item_keys=HTML_ATT")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["target_documents"] == 1
            assert data["eligible_documents"] == 1
            assert data["total_pages"] == 0
            assert data["estimated_cost_usd"] == 0.0

    def test_cost_estimate_incremental_skips_already_converted(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        dummy_pdf = tmp_path / "converted.pdf"
        dummy_pdf.write_text("pdf content", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "CONV_KEY"
        mock_att.content_type = "application/pdf"
        mock_att.path = dummy_pdf

        from zocrates.rag.document_processor import _file_sha256

        file_hash = _file_sha256(dummy_pdf)

        mock_syncer.db.get_attachment_by_key.return_value = mock_att
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "datalab",
            "source_hash": file_hash,
        }

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/cost-estimate?converter_id=datalab&item_keys=CONV_KEY")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["target_documents"] == 1
            assert data["already_converted_count"] == 1
            assert data["eligible_documents"] == 0
            assert data["total_pages"] == 0
            assert data["estimated_cost_usd"] == 0.0

            # When force=true, already converted is included
            res_forced = client.get("/cost-estimate?converter_id=datalab&item_keys=CONV_KEY&force=true")
            assert res_forced.status_code == 200
            data_forced = res_forced.json()
            assert data_forced["already_converted_count"] == 0
            assert data_forced["eligible_documents"] == 1

    def test_cost_estimate_datalab_merge_cross_page_flag(self, client, tmp_path) -> None:
        """Verify datalab_merge_cross_page is present and reflects configured value."""
        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = tmp_path
        mock_syncer.db.get_all_convertible_attachments.return_value = []

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.config.get_datalab_merge_cross_page", return_value=True):
                res = client.get("/cost-estimate?converter_id=datalab")
                assert res.status_code == 200
                data = res.json()
                assert "datalab_merge_cross_page" in data
                assert data["datalab_merge_cross_page"] is True

            with patch("zocrates.config.get_datalab_merge_cross_page", return_value=False):
                res = client.get("/cost-estimate?converter_id=datalab")
                assert res.status_code == 200
                data = res.json()
                assert "datalab_merge_cross_page" in data
                assert data["datalab_merge_cross_page"] is False


class TestBatchConvertEndpoint:
    """Verify POST /batch-convert endpoint."""

    def test_batch_convert_unregistered_returns_400(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/batch-convert", json={"item_keys": ["KEY1"]})
            assert res.status_code == 400

    def test_batch_convert_unknown_converter_returns_400(self, client) -> None:
        mock_syncer = MagicMock()
        mock_syncer.processor._get_converter_by_id.side_effect = KeyError("unknown_conv")

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": ["KEY1"], "converter_id": "unknown_conv"})
            assert res.status_code == 400
            data = res.json()
            assert data["detail"]["error"] == "unknown_converter"

    def test_batch_convert_empty_keys(self, client) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "markitdown"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor._document_converter_id = "markitdown"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": []})
            assert res.status_code == 200
            events = parse_sse_events(res.text)
            assert len(events) == 1
            assert events[0]["event"] == "complete"
            assert events[0]["data"]["total"] == 0

    def test_batch_convert_successful_serial_execution(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "markitdown"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor._document_converter_id = "markitdown"
        mock_syncer.processor.convert_with.return_value = "# Header\n\nParagraph text."
        mock_syncer.processor.get_metadata.return_value = None

        pdf1 = tmp_path / "paper1.pdf"
        pdf1.write_text("pdf 1 content", encoding="utf-8")
        pdf2 = tmp_path / "paper2.pdf"
        pdf2.write_text("pdf 2 content", encoding="utf-8")

        att1 = MagicMock()
        att1.item_key = "ATT1"
        att1.content_type = "application/pdf"
        att1.path = pdf1
        att1.parent_item_id = 10
        att1.item_id = 1
        att1.parent_item_key = "PARENT1"

        att2 = MagicMock()
        att2.item_key = "ATT2"
        att2.content_type = "application/pdf"
        att2.path = pdf2
        att2.parent_item_id = 20
        att2.item_id = 2
        att2.parent_item_key = "PARENT2"

        def get_att_by_key(k):
            if k == "ATT1":
                return att1
            if k == "ATT2":
                return att2
            return None

        mock_syncer.db.get_attachment_by_key.side_effect = get_att_by_key
        mock_syncer.db.get_item.return_value = MagicMock(title="Mock Title", item_type="journalArticle", collections=[])

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": ["ATT1", "ATT2"], "converter_id": "markitdown"})
            assert res.status_code == 200
            events = parse_sse_events(res.text)

            assert len(events) == 7
            # ATT1: converting -> indexing -> completed
            assert events[0]["event"] == "progress"
            assert events[0]["data"]["status"] == "converting"
            assert events[0]["data"]["current_key"] == "ATT1"
            assert events[0]["data"]["current_index"] == 1

            assert events[1]["event"] == "progress"
            assert events[1]["data"]["status"] == "indexing"
            assert events[1]["data"]["current_key"] == "ATT1"

            assert events[2]["event"] == "progress"
            assert events[2]["data"]["status"] == "completed"
            assert events[2]["data"]["current_key"] == "ATT1"
            assert events[2]["data"]["processed"] == 1

            # ATT2: converting -> indexing -> completed
            assert events[3]["event"] == "progress"
            assert events[3]["data"]["status"] == "converting"
            assert events[3]["data"]["current_key"] == "ATT2"
            assert events[3]["data"]["current_index"] == 2

            assert events[4]["event"] == "progress"
            assert events[4]["data"]["status"] == "indexing"
            assert events[4]["data"]["current_key"] == "ATT2"

            assert events[5]["event"] == "progress"
            assert events[5]["data"]["status"] == "completed"
            assert events[5]["data"]["current_key"] == "ATT2"
            assert events[5]["data"]["processed"] == 2

            assert events[6]["event"] == "complete"
            assert events[6]["data"]["total"] == 2
            assert events[6]["data"]["processed"] == 2
            assert events[6]["data"]["failed"] == 0

            # Ingestion called for each document
            assert mock_syncer.store.ingest_document.call_count == 2
            assert mock_syncer.processor.update_indexed_at.call_count == 2

    def test_batch_convert_skips_already_converted(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "datalab"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor.convert_with.return_value = "# Doc"

        pdf1 = tmp_path / "already_conv.pdf"
        pdf1.write_text("pdf 1 content", encoding="utf-8")

        from zocrates.rag.document_processor import _file_sha256

        file_hash = _file_sha256(pdf1)

        att = MagicMock()
        att.item_key = "ATT_CONV"
        att.content_type = "application/pdf"
        att.path = pdf1
        att.parent_item_id = None
        att.item_id = 1
        att.parent_item_key = None

        mock_syncer.db.get_attachment_by_key.return_value = att
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "datalab",
            "source_hash": file_hash,
        }

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            # Without force: skipped
            res = client.post("/batch-convert", json={"item_keys": ["ATT_CONV"], "converter_id": "datalab", "force": False})
            assert res.status_code == 200
            events = parse_sse_events(res.text)
            assert len(events) == 2
            assert events[0]["event"] == "progress"
            assert events[0]["data"]["status"] == "skipped"
            assert events[1]["event"] == "complete"
            assert events[1]["data"]["skipped"] == 1
            assert events[1]["data"]["processed"] == 0
            assert mock_syncer.processor.convert_with.call_count == 0

    def test_batch_convert_force_reconversion(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "datalab"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor.convert_with.return_value = "# Doc"

        pdf1 = tmp_path / "already_conv.pdf"
        pdf1.write_text("pdf 1 content", encoding="utf-8")

        from zocrates.rag.document_processor import _file_sha256

        file_hash = _file_sha256(pdf1)

        att = MagicMock()
        att.item_key = "ATT_CONV"
        att.content_type = "application/pdf"
        att.path = pdf1
        att.parent_item_id = None
        att.item_id = 1
        att.parent_item_key = None

        mock_syncer.db.get_attachment_by_key.return_value = att
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "datalab",
            "source_hash": file_hash,
        }

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            # With force=True: converted (converting -> indexing -> completed -> complete)
            res_forced = client.post("/batch-convert", json={"item_keys": ["ATT_CONV"], "converter_id": "datalab", "force": True})
            assert res_forced.status_code == 200
            events_forced = parse_sse_events(res_forced.text)
            assert len(events_forced) == 4
            assert events_forced[0]["data"]["status"] == "converting"
            assert events_forced[1]["data"]["status"] == "indexing"
            assert events_forced[2]["data"]["status"] == "completed"
            assert events_forced[2]["data"]["processed"] == 1
            assert events_forced[3]["event"] == "complete"
            assert events_forced[3]["data"]["processed"] == 1
            assert mock_syncer.processor.convert_with.call_count == 1

    def test_batch_convert_parent_item_key_resolution(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "markitdown"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor._document_converter_id = "markitdown"
        mock_syncer.processor.convert_with.return_value = "# Resolved Doc"
        mock_syncer.processor.get_metadata.return_value = None

        pdf1 = tmp_path / "child.pdf"
        pdf1.write_text("pdf bytes", encoding="utf-8")

        mock_parent = MagicMock(item_id=77, title="Parent Paper Title", item_type="journalArticle", collections=[])
        mock_child_att = MagicMock(item_key="CHILD_ATT_KEY", content_type="application/pdf", path=pdf1, parent_item_id=77, item_id=88, parent_item_key="PARENT_KEY")

        mock_syncer.db.get_attachment_by_key.return_value = None
        mock_syncer.db.get_item_by_key.return_value = mock_parent
        mock_syncer.db.get_attachments.return_value = [mock_child_att]

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": ["PARENT_KEY"], "converter_id": "markitdown"})
            assert res.status_code == 200
            events = parse_sse_events(res.text)
            assert len(events) == 4
            assert events[0]["event"] == "progress"
            assert events[0]["data"]["status"] == "converting"
            assert events[1]["event"] == "progress"
            assert events[1]["data"]["status"] == "indexing"
            assert events[2]["event"] == "progress"
            assert events[2]["data"]["status"] == "completed"
            assert events[2]["data"]["current_key"] == "CHILD_ATT_KEY"
            assert events[2]["data"]["current_title"] == "Parent Paper Title"
            assert events[3]["event"] == "complete"
            assert events[3]["data"]["processed"] == 1

    def test_batch_convert_datalab_routes_non_pdf_to_fallback(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "datalab"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv
        mock_syncer.processor.datalab_non_pdf_fallback = "markitdown"
        mock_syncer.processor.convert_with.return_value = "# Fallback Converted HTML"

        html_file = tmp_path / "webpage.html"
        html_file.write_text("<html><body>content</body></html>", encoding="utf-8")

        att = MagicMock()
        att.item_key = "ATT_HTML"
        att.content_type = "text/html"
        att.path = html_file
        att.parent_item_id = None
        att.item_id = 1
        att.parent_item_key = None

        mock_syncer.db.get_attachment_by_key.return_value = att
        mock_syncer.processor.get_metadata.return_value = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": ["ATT_HTML"], "converter_id": "datalab"})
            assert res.status_code == 200
            events = parse_sse_events(res.text)
            # Should not skip; should convert using fallback
            assert len(events) == 4
            assert events[0]["event"] == "progress"
            assert events[0]["data"]["status"] == "converting"
            assert events[1]["event"] == "progress"
            assert events[1]["data"]["status"] == "indexing"
            assert events[2]["event"] == "progress"
            assert events[2]["data"]["status"] == "completed"
            assert events[3]["event"] == "complete"
            assert events[3]["data"]["processed"] == 1
            # Verify convert_with was called with markitdown fallback
            call_kwargs = mock_syncer.processor.convert_with.call_args[1]
            assert call_kwargs["converter_id"] == "markitdown"

    def test_batch_convert_partial_failure_isolation(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        mock_conv = MagicMock()
        mock_conv.converter_id = "docling_local"
        mock_syncer.processor._get_converter_by_id.return_value = mock_conv

        pdf1 = tmp_path / "bad.pdf"
        pdf1.write_text("bad pdf", encoding="utf-8")
        pdf2 = tmp_path / "good.pdf"
        pdf2.write_text("good pdf", encoding="utf-8")

        att1 = MagicMock(item_key="ATT_BAD", content_type="application/pdf", path=pdf1, parent_item_id=None, item_id=1, parent_item_key=None)
        att2 = MagicMock(item_key="ATT_GOOD", content_type="application/pdf", path=pdf2, parent_item_id=None, item_id=2, parent_item_key=None)

        def get_att(k):
            if k == "ATT_BAD":
                return att1
            if k == "ATT_GOOD":
                return att2
            return None

        mock_syncer.db.get_attachment_by_key.side_effect = get_att
        mock_syncer.processor.get_metadata.return_value = None

        def convert_side_effect(item_key, **kwargs):
            if item_key == "ATT_BAD":
                raise RuntimeError("Corrupted PDF structure")
            return "# Good Doc\n\nContent"

        mock_syncer.processor.convert_with.side_effect = convert_side_effect

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-convert", json={"item_keys": ["ATT_BAD", "ATT_GOOD"], "converter_id": "docling_local"})
            assert res.status_code == 200
            events = parse_sse_events(res.text)

            # ATT_BAD: converting -> failed; ATT_GOOD: converting -> indexing -> completed; complete
            assert len(events) == 6
            assert events[0]["event"] == "progress"
            assert events[0]["data"]["status"] == "converting"
            assert events[0]["data"]["current_key"] == "ATT_BAD"

            assert events[1]["event"] == "progress"
            assert events[1]["data"]["status"] == "failed"
            assert "Corrupted PDF" in events[1]["data"]["error"]

            assert events[2]["event"] == "progress"
            assert events[2]["data"]["status"] == "converting"
            assert events[2]["data"]["current_key"] == "ATT_GOOD"

            assert events[3]["event"] == "progress"
            assert events[3]["data"]["status"] == "indexing"
            assert events[3]["data"]["current_key"] == "ATT_GOOD"

            assert events[4]["event"] == "progress"
            assert events[4]["data"]["status"] == "completed"
            assert events[4]["data"]["current_key"] == "ATT_GOOD"

            assert events[5]["event"] == "complete"
            assert events[5]["data"]["total"] == 2
            assert events[5]["data"]["processed"] == 1
            assert events[5]["data"]["failed"] == 1
            assert len(events[5]["data"]["failed_items"]) == 1
            assert events[5]["data"]["failed_items"][0]["item_key"] == "ATT_BAD"

            mock_syncer.processor.record_failure.assert_called_once()


class TestLLMEndpoints:
    """Verify top-level /llm/... endpoints."""

    def test_get_config_includes_auto_spawn_backend(self, client, tmp_path) -> None:
        """GET /config must expose auto_spawn_backend (default True, honoring
        explicit dev-profile opt-out) so the frontend can mirror the gate."""
        from zocrates.config import save_zocrates_config
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            res = client.get("/config")
            assert res.status_code == 200
            data = res.json()
            assert "auto_spawn_backend" in data
            assert data["auto_spawn_backend"] is True

            save_zocrates_config({"auto_spawn_backend": False}, zotero_data_dir=tmp_path)
            res2 = client.get("/config")
            assert res2.status_code == 200
            assert res2.json()["auto_spawn_backend"] is False

    def test_get_llm_config(self, client, tmp_path) -> None:
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            res = client.get("/config")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "llm_provider" in data
            assert "llm_model" in data
            assert data.get("llm_num_retries") == 4
            assert "has_openrouter_key" in data
            assert "override_agent_models" in data
            assert "coordinator" in data["override_agent_models"]

    def test_post_llm_config(self, client, tmp_path) -> None:
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            res = client.post(
                "/config",
                json={
                    "llm_provider": "openrouter_llm",
                    "llm_model": "google/gemini-3.8-flash",
                    "llm_num_retries": 5,
                    "override_agent_models": {
                        "library_search": {"model": None, "thinking_level": "low"}
                    },
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["config"]["llm_num_retries"] == 5
            assert data["config"]["override_agent_models"]["library_search"]["model"] is None
            assert "coordinator" in data["config"]["override_agent_models"]
            assert "library_synthesizer" in data["config"]["override_agent_models"]

    def test_config_endpoint_get_and_post(self, client, tmp_path) -> None:
        """GET /config returns the active configuration and POST /config updates
        and persists the same fields."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            get_res = client.get("/config")
            assert get_res.status_code == 200
            assert get_res.json()["status"] == "success"

            post_payload = {
                "llm_provider": "openrouter_llm",
                "llm_model": "google/gemini-3.8-flash",
                "llm_num_retries": 7,
            }
            post_res = client.post("/config", json=post_payload)
            assert post_res.status_code == 200
            assert post_res.json()["config"]["llm_num_retries"] == 7

            # The POST must be persisted to the same zocrates_config.json
            import json as _json
            cfg_file = tmp_path / "zocrates-data" / "zocrates_config.json"
            if not cfg_file.exists():
                cfg_file = tmp_path / "zocrates_config.json"
            disk = _json.loads(cfg_file.read_text())
            assert disk["llm_num_retries"] == 7

            # A subsequent GET must reflect the state written via POST.
            get_after = client.get("/config")
            assert get_after.status_code == 200
            assert get_after.json()["llm_num_retries"] == 7

            # The retired /llm/config route must return 404
            assert client.get("/llm/config").status_code == 404
            assert client.post("/llm/config", json=post_payload).status_code == 404

    def test_post_config_updates_datalab_api_key(self, client, tmp_path) -> None:
        """POST /config updates datalab_api_key in memory via save_datalab_api_key."""
        from zocrates.config import get_datalab_api_key

        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"), \
             patch("zocrates.config.save_datalab_api_key") as mock_save_datalab:
            res = client.post(
                "/config",
                json={"datalab_api_key": "  dl-test-key-xyz123  "},
            )
            assert res.status_code == 200
            assert res.json()["status"] == "success"
            mock_save_datalab.assert_called_once_with("dl-test-key-xyz123")

        # Also test with empty string (clearing the key)
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"), \
             patch("zocrates.config.save_datalab_api_key") as mock_save_datalab:
            res = client.post(
                "/config",
                json={"datalab_api_key": ""},
            )
            assert res.status_code == 200
            mock_save_datalab.assert_called_once_with(None)

        # Verify live in-memory update with real save_datalab_api_key
        orig_key = os.environ.get("DATALAB_API_KEY")
        try:
            with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
                 patch("zocrates.agents.app.set_agent_models"):
                res = client.post(
                    "/config",
                    json={"datalab_api_key": "live-sync-key-456"},
                )
                assert res.status_code == 200
                assert get_datalab_api_key() == "live-sync-key-456"
        finally:
            if orig_key is not None:
                os.environ["DATALAB_API_KEY"] = orig_key
            else:
                os.environ.pop("DATALAB_API_KEY", None)

    def test_post_llm_config_persists_reranker_model(self, client, tmp_path) -> None:
        """Phase 7 Stage B: reranker_model is accepted by /config and
        persisted (the onboarding wizard's Free preset needs the free reranker
        slug to reach disk — Pydantic v2 would otherwise silently drop it)."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            res = client.post(
                "/config",
                json={"reranker_model": "nvidia/llama-nemotron-rerank-vl-1b-v2:free"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["config"]["reranker_model"] == "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
            # Persisted to disk
            import json as _json
            cfg_file = tmp_path / "zocrates-data" / "zocrates_config.json"
            if not cfg_file.exists():
                cfg_file = tmp_path / "zocrates_config.json"
            disk = _json.loads(cfg_file.read_text())
            assert disk["reranker_model"] == "nvidia/llama-nemotron-rerank-vl-1b-v2:free"

    def test_get_openrouter_models_endpoint(self, client) -> None:
        res = client.get("/llm/openrouter_models")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data
        assert "models" in data

    def test_get_openrouter_models_decision_endpoint(self, client) -> None:
        with patch("zocrates.config.get_secure_api_key", return_value="dummy-key"), \
             patch("zocrates.llm.get_openrouter_models", return_value=[{"id": "inception/mercury-decide", "name": "Inception: Mercury Decide"}]) as mock_get:
            res = client.get("/llm/openrouter_models?target_type=decision")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert len(data["models"]) == 1
            assert data["models"][0]["id"] == "inception/mercury-decide"
            mock_get.assert_called_once()
            assert mock_get.call_args.kwargs["target_type"] == "decision"

    def test_post_llm_test_endpoint(self, client, tmp_path) -> None:
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.llm.test_llm_connection", return_value={"status": "ok", "message": "Connection successful!", "model": "mock-model"}):
            res = client.post("/llm/test")
            assert res.status_code == 200
            data = res.json()
            assert "status" in data
            assert "model" in data

    def test_post_llm_test_openrouter_key_missing(self, client) -> None:
        """POST /llm/test_openrouter_key returns error when key is missing."""
        with patch("zocrates.config.get_secure_api_key", return_value=None):
            res = client.post("/llm/test_openrouter_key", json={"openrouter_api_key": ""})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "OpenRouter API key is missing. Please enter an API key." == data["message"]

    def test_post_llm_test_openrouter_key_success(self, client) -> None:
        """POST /llm/test_openrouter_key returns success when OpenRouter reports valid key."""
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"limit": 10.0, "usage": 0.05}}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = client.post(
                "/llm/test_openrouter_key",
                json={"openrouter_api_key": "sk-or-test-valid-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "Connected to OpenRouter successfully. API key is valid." in data["message"]
            assert "Limit: $10.0" in data["message"]
            assert "Usage: $0.05" in data["message"]
            assert data["data"] == {"limit": 10.0, "usage": 0.05}

    def test_post_llm_test_openrouter_key_unauthorized(self, client) -> None:
        """POST /llm/test_openrouter_key returns error when 401 Unauthorized is returned."""
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": {"message": "Invalid API key provided", "code": 401}}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = client.post(
                "/llm/test_openrouter_key",
                json={"openrouter_api_key": "sk-or-invalid-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "rejected (401 Unauthorized): Invalid API key provided" in data["message"]

    def test_post_llm_test_openrouter_key_network_error(self, client) -> None:
        """POST /llm/test_openrouter_key returns clean error message on network failure."""
        import httpx
        from unittest.mock import AsyncMock

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Network connection refused")
            res = client.post(
                "/llm/test_openrouter_key",
                json={"openrouter_api_key": "sk-or-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "Failed to connect to OpenRouter: Network connection refused" in data["message"]

    def test_get_llm_config_includes_citations_fields(self, client, tmp_path) -> None:
        """GET /config returns citations_enabled and references_section_enabled with defaults."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            res = client.get("/config")
            assert res.status_code == 200
            data = res.json()
            assert "citations_enabled" in data
            assert "references_section_enabled" in data
            # Defaults must be True per DEFAULT_CITATIONS_ENABLED / DEFAULT_REFERENCES_SECTION_ENABLED
            assert data["citations_enabled"] is True
            assert data["references_section_enabled"] is True

    def test_post_llm_config_updates_citations_fields(self, client, tmp_path) -> None:
        """POST /config persists citations_enabled and references_section_enabled to zocrates_config.json."""
        import json

        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        config_path = config_dir / "zocrates_config.json"

        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            # Disable citations and references
            res = client.post(
                "/config",
                json={"citations_enabled": False, "references_section_enabled": False},
            )
            assert res.status_code == 200

            # Confirm persisted to disk
            saved = json.loads(config_path.read_text())
            assert saved["citations_enabled"] is False
            assert saved["references_section_enabled"] is False

            # Confirm GET reflects the new values
            res2 = client.get("/config")
            assert res2.status_code == 200
            data2 = res2.json()
            assert data2["citations_enabled"] is False
            assert data2["references_section_enabled"] is False

            # Re-enable only citations
            res3 = client.post(
                "/config",
                json={"citations_enabled": True},
            )
            assert res3.status_code == 200
            saved2 = json.loads(config_path.read_text())
            assert saved2["citations_enabled"] is True
            assert saved2["references_section_enabled"] is False

    def test_get_llm_config_includes_auto_render_mermaid_diagrams(self, client, tmp_path) -> None:
        """GET /config returns auto_render_mermaid_diagrams with default True."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            res = client.get("/config")
            assert res.status_code == 200
            data = res.json()
            assert "auto_render_mermaid_diagrams" in data
            assert data["auto_render_mermaid_diagrams"] is True

    def test_post_llm_config_updates_auto_render_mermaid_diagrams(self, client, tmp_path) -> None:
        """POST /config persists auto_render_mermaid_diagrams to zocrates_config.json."""
        import json

        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        config_path = config_dir / "zocrates_config.json"

        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            # Disable auto render
            res = client.post(
                "/config",
                json={"auto_render_mermaid_diagrams": False},
            )
            assert res.status_code == 200
            saved = json.loads(config_path.read_text())
            assert saved["auto_render_mermaid_diagrams"] is False

            # Confirm GET reflects new value
            res2 = client.get("/config")
            assert res2.status_code == 200
            assert res2.json()["auto_render_mermaid_diagrams"] is False

            # Re-enable
            res3 = client.post(
                "/config",
                json={"auto_render_mermaid_diagrams": True},
            )
            assert res3.status_code == 200
            saved2 = json.loads(config_path.read_text())
            assert saved2["auto_render_mermaid_diagrams"] is True

    def test_load_config_backfills_auto_render_mermaid_diagrams(self, tmp_path) -> None:
        """load_zocrates_config backfills auto_render_mermaid_diagrams=True if missing on disk."""
        import json
        from zocrates.config import get_auto_render_mermaid_diagrams, load_zocrates_config

        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        config_path = config_dir / "zocrates_config.json"
        config_path.write_text(json.dumps({"llm_model": "test-model"}))

        cfg = load_zocrates_config(tmp_path)
        assert cfg["auto_render_mermaid_diagrams"] is True
        assert get_auto_render_mermaid_diagrams(tmp_path) is True

        # Check persisted backfill
        persisted = json.loads(config_path.read_text())
        assert persisted["auto_render_mermaid_diagrams"] is True

    def test_openalex_api_key_status_and_config(self, client, tmp_path) -> None:
        """GET /status and GET /config reflect has_openalex_api_key."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            with patch("zocrates.config.get_openalex_api_key", return_value=None):
                res_status = client.get("/status")
                assert res_status.status_code == 200
                assert res_status.json()["has_openalex_api_key"] is False

                res_cfg = client.get("/config")
                assert res_cfg.status_code == 200
                assert res_cfg.json()["has_openalex_api_key"] is False

            with patch("zocrates.config.get_openalex_api_key", return_value="some-key"):
                res_status = client.get("/status")
                assert res_status.status_code == 200
                assert res_status.json()["has_openalex_api_key"] is True

                res_cfg = client.get("/config")
                assert res_cfg.status_code == 200
                assert res_cfg.json()["has_openalex_api_key"] is True

    def test_post_llm_config_openalex_api_key(self, client, tmp_path) -> None:
        """POST /config saves openalex_api_key securely."""
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.config.save_openalex_api_key") as mock_save:
            res = client.post(
                "/config",
                json={"openalex_api_key": "test_openalex_key_123"},
            )
            assert res.status_code == 200
            mock_save.assert_called_once_with("test_openalex_key_123")

    def test_discovery_test_openalex_missing_key(self, client) -> None:
        """POST /discovery/test_openalex returns error when key is missing."""
        with patch("zocrates.config.get_openalex_api_key", return_value=None):
            res = client.post("/discovery/test_openalex", json={"openalex_api_key": ""})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "OpenAlex API key is missing" in data["message"]

    def test_discovery_test_openalex_valid_key(self, client) -> None:
        """POST /discovery/test_openalex returns success when 200 returned."""
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"rate_limit": 100000}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = client.post(
                "/discovery/test_openalex",
                json={"openalex_api_key": "valid_openalex_key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "Connected to OpenAlex API successfully" in data["message"]
            assert data["rate_limit"] == {"rate_limit": 100000}

    def test_discovery_test_openalex_rejected_key(self, client) -> None:
        """POST /discovery/test_openalex returns error when 401 returned."""
        from unittest.mock import AsyncMock, MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = client.post(
                "/discovery/test_openalex",
                json={"openalex_api_key": "invalid_key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "error"
            assert "rejected" in data["message"].lower()

    def test_post_register_openalex_api_key(self, client, tmp_path) -> None:
        """POST /register saves openalex_api_key when provided."""
        (tmp_path / "zotero.sqlite").touch()
        (tmp_path / "storage").mkdir()

        with patch("zocrates.fast_api_app.initialize_services"), \
             patch("zocrates.config.save_openalex_api_key") as mock_save, \
             patch("zocrates.fast_api_app.setup_file_logging"), \
             patch("zocrates.utils.services.get_session_service"), \
             patch("zocrates.utils.services.get_artifact_service"):
            res = client.post(
                "/register",
                json={
                    "zotero_data_dir": str(tmp_path),
                    "openalex_api_key": "registered_alex_key",
                },
            )
            assert res.status_code == 200
            mock_save.assert_called_once_with("registered_alex_key")

    def test_post_register_openrouter_api_key(self, client, tmp_path) -> None:
        """POST /register saves openrouter_api_key when provided."""
        (tmp_path / "zotero.sqlite").touch()
        (tmp_path / "storage").mkdir()

        with patch("zocrates.fast_api_app.initialize_services"), \
             patch("zocrates.config.save_secure_api_key") as mock_save, \
             patch("zocrates.fast_api_app.setup_file_logging"), \
             patch("zocrates.utils.services.get_session_service"), \
             patch("zocrates.utils.services.get_artifact_service"):
            res = client.post(
                "/register",
                json={
                    "zotero_data_dir": str(tmp_path),
                    "openrouter_api_key": "registered_openrouter_key",
                },
            )
            assert res.status_code == 200
            mock_save.assert_called_once_with(
                "registered_openrouter_key",
                key_name="openrouter_api_key",
                env_var="OPENROUTER_API_KEY",
            )

    def test_post_register_datalab_api_key(self, client, tmp_path) -> None:
        """POST /register saves datalab_api_key when provided."""
        (tmp_path / "zotero.sqlite").touch()
        (tmp_path / "storage").mkdir()

        with patch("zocrates.fast_api_app.initialize_services"), \
             patch("zocrates.config.save_datalab_api_key") as mock_save, \
             patch("zocrates.fast_api_app.setup_file_logging"), \
             patch("zocrates.utils.services.get_session_service"), \
             patch("zocrates.utils.services.get_artifact_service"):
            res = client.post(
                "/register",
                json={
                    "zotero_data_dir": str(tmp_path),
                    "datalab_api_key": "registered_datalab_key",
                },
            )
            assert res.status_code == 200
            mock_save.assert_called_once_with("registered_datalab_key")

    def test_post_register_empty_keys_not_saved(self, client, tmp_path) -> None:
        """POST /register ignores empty or whitespace-only keys."""
        (tmp_path / "zotero.sqlite").touch()
        (tmp_path / "storage").mkdir()

        with patch("zocrates.fast_api_app.initialize_services"), \
             patch("zocrates.config.save_secure_api_key") as mock_save_secure, \
             patch("zocrates.config.save_datalab_api_key") as mock_save_datalab, \
             patch("zocrates.config.save_openalex_api_key") as mock_save_alex, \
             patch("zocrates.fast_api_app.setup_file_logging"), \
             patch("zocrates.utils.services.get_session_service"), \
             patch("zocrates.utils.services.get_artifact_service"):
            res = client.post(
                "/register",
                json={
                    "zotero_data_dir": str(tmp_path),
                    "openrouter_api_key": "   ",
                    "datalab_api_key": "",
                    "openalex_api_key": " ",
                },
            )
            assert res.status_code == 200
            mock_save_secure.assert_not_called()
            mock_save_datalab.assert_not_called()
            mock_save_alex.assert_not_called()



class TestItemStatusEndpoint:
    """Verify GET /items/{item_key}/status endpoint."""

    def test_item_status_unregistered_handles_gracefully(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/items/ITEM123/status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["is_cached"] is False
            assert data["is_indexed"] is False
            assert data["metadata"] is None

    def test_item_status_success(self, client) -> None:
        mock_syncer = MagicMock()
        mock_syncer.processor.is_cached.return_value = True
        mock_syncer.store.is_ingested.return_value = True
        mock_syncer.processor.get_metadata.return_value = {
            "converter_id": "markitdown",
            "converter_version": "1.0.0",
            "source_path": "/path/to/paper.pdf",
            "content_type": "application/pdf",
            "parent_item_type": "journalArticle",
            "converted_at": "2026-03-30T14:20:00Z",
            "indexed_at": "2026-03-30T14:20:05Z",
            "source_hash": "abc123hash",
            "char_count": 1234,
        }
        mock_syncer.processor.get_failures.return_value = {
            "docling_local": {
                "error_message": "CUDA out of memory",
                "source_hash": "abc123hash",
                "failed_at": "2026-03-30T14:20:10Z",
            }
        }

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/items/ITEM123/status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["item_key"] == "ITEM123"
            assert data["is_cached"] is True
            assert data["is_indexed"] is True
            assert data["metadata"]["converter_id"] == "markitdown"
            assert data["metadata"]["char_count"] == 1234
            assert "failed_converters" in data
            assert "docling_local" in data["failed_converters"]
            assert data["failed_converters"]["docling_local"]["error_message"] == "CUDA out of memory"


class TestDoclingModeAndTimeoutEndpoints:
    """Verify docling_mode configuration in /rag/converter and timeout behavior."""

    def test_post_rag_converter_updates_docling_mode(self, client) -> None:
        mock_syncer = MagicMock()
        mock_syncer.processor._document_converter_id = "docling_local"
        mock_syncer._zotero_data_dir = None

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("zocrates.config.save_zocrates_config") as mock_save:
                res = client.post(
                    "/rag/converter",
                    json={
                        "document_converter_id": "docling_local",
                        "document_converter_mode": "local",
                        "docling_mode": "fast",
                        "datalab_non_pdf_fallback": "docling_local",
                    },
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "success"
                assert data["document_converter_id"] == "docling_local"
                assert data["docling_mode"] == "fast"
                assert data["datalab_non_pdf_fallback"] == "docling_local"
                assert mock_syncer.processor.datalab_non_pdf_fallback == "docling_local"
                mock_save.assert_called_once_with(
                    {
                        "document_converter_id": "docling_local",
                        "document_converter_mode": "local",
                        "docling_mode": "fast",
                        "datalab_non_pdf_fallback": "docling_local",
                    }
                )

    def test_datalab_merge_cross_page_endpoints(self, client, tmp_path) -> None:
        """Verify datalab_merge_cross_page is exposed and updatable via /status, /config, and /rag/converter."""
        # 1. /status endpoint (registered state)
        res = client.get("/status")
        assert res.status_code == 200
        assert "datalab_merge_cross_page" in res.json()
        assert isinstance(res.json()["datalab_merge_cross_page"], bool)

        # Unregistered /status branch
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res_unreg = client.get("/status")
            assert res_unreg.status_code == 200
            assert "datalab_merge_cross_page" in res_unreg.json()
            assert isinstance(res_unreg.json()["datalab_merge_cross_page"], bool)

        # 2. /config endpoint GET & POST
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
             patch("zocrates.agents.app.set_agent_models"):
            cfg_res = client.get("/config")
            assert cfg_res.status_code == 200
            assert "datalab_merge_cross_page" in cfg_res.json()
            assert cfg_res.json()["datalab_merge_cross_page"] is False

            post_cfg = client.post("/config", json={"datalab_merge_cross_page": True})
            assert post_cfg.status_code == 200
            assert post_cfg.json()["config"]["datalab_merge_cross_page"] is True

            cfg_after = client.get("/config")
            assert cfg_after.status_code == 200
            assert cfg_after.json()["datalab_merge_cross_page"] is True

        # 3. /rag/converter endpoint POST
        mock_syncer = MagicMock()
        mock_syncer.processor._document_converter_id = "datalab"
        mock_syncer._zotero_data_dir = tmp_path
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.config.save_zocrates_config") as mock_save, \
             patch("zocrates.config.get_datalab_merge_cross_page", return_value=True):
            res_conv = client.post(
                "/rag/converter",
                json={
                    "document_converter_id": "datalab",
                    "datalab_merge_cross_page": True,
                },
            )
            assert res_conv.status_code == 200
            data_conv = res_conv.json()
            assert data_conv["status"] == "success"
            assert data_conv["datalab_merge_cross_page"] is True
            mock_save.assert_called_once_with(
                {
                    "document_converter_id": "datalab",
                    "datalab_merge_cross_page": True,
                }
            )

    def test_status_endpoint_includes_docling_assessment_and_mode(self, client) -> None:
        res = client.get("/status")
        assert res.status_code == 200
        data = res.json()
        assert "hardware" in data
        assert "docling_assessment" in data["hardware"]
        assert "recommended_mode" in data["hardware"]["docling_assessment"]
        assert "docling_mode" in data
        assert "datalab_non_pdf_fallback" in data
        assert "datalab_merge_cross_page" in data
        assert isinstance(data["datalab_merge_cross_page"], bool)
        assert "enable_vlm_captioning" in data
        assert "vlm_captioning_provider" in data
        assert "vlm_captioning_model" in data
        assert "vlm_captioning_backup_model" in data
        assert "vlm_captioning_local_model" in data
        assert "vlm_max_images_per_doc" in data
        assert "auto_render_mermaid_diagrams" in data
        assert data["auto_render_mermaid_diagrams"] is True
        assert "auto_spawn_backend" in data
        assert isinstance(data["auto_spawn_backend"], bool)
        assert "project_dir" in data
        assert isinstance(data["project_dir"], str)
        assert len(data["project_dir"]) > 0

    def test_status_endpoint_includes_project_dir_unregistered_and_registered(self, client) -> None:
        """Verify project_dir is returned in /status response for both registered and unregistered states."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "not_registered"
            assert "project_dir" in data
            assert Path(data["project_dir"]).is_dir()
            # The launcher's attach classifier compares this field against
            # Zotero.DataDirectory.dir even pre-registration; it must always
            # be present (None when nothing can be resolved).
            assert "zotero_data_dir" in data

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/mock_zotero")
        mock_syncer.processor.list_cached_keys.return_value = []
        mock_syncer.store._embedder = MagicMock(dimension=768, _model_name="jinaai/jina-embeddings-v5-text-nano")
        mock_syncer.store.active_collection_name = "test_collection"
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ready"
            assert "project_dir" in data
            assert Path(data["project_dir"]).is_dir()

    def test_post_rag_converter_updates_vlm_settings(self, client) -> None:
        with patch("zocrates.config.save_zocrates_config") as mock_save:
            res = client.post(
                "/rag/converter",
                json={
                    "enable_vlm_captioning": True,
                    "vlm_captioning_provider": "ollama",
                    "vlm_captioning_local_model": "llava:latest",
                    "vlm_max_images_per_doc": 15,
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            mock_save.assert_called_once()
            args, _ = mock_save.call_args
            updates = args[0]
            assert updates["enable_vlm_captioning"] is True
            assert updates["vlm_captioning_provider"] == "ollama"
            assert updates["vlm_captioning_local_model"] == "llava:latest"
            assert updates["vlm_max_images_per_doc"] == 15

    def test_convert_item_timeout_returns_504(self, client, tmp_path) -> None:
        import asyncio

        mock_syncer = MagicMock()
        mock_syncer.processor._document_converter_id = "docling_local"
        dummy_pdf = tmp_path / "hang.pdf"
        dummy_pdf.write_text("dummy", encoding="utf-8")

        mock_att = MagicMock()
        mock_att.item_key = "HANG_KEY"
        mock_att.path = dummy_pdf
        mock_att.content_type = "application/pdf"
        mock_att.parent_item_id = None
        mock_att.parent_item_key = None

        mock_syncer.db.get_attachment_by_key.return_value = mock_att

        async def _mock_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            with patch("asyncio.wait_for", side_effect=_mock_timeout):
                res = client.post("/convert/HANG_KEY")
                assert res.status_code == 504
                data = res.json()
                assert "timed out" in data["detail"]["message"]
                mock_syncer.processor.record_failure.assert_called_once()


class TestLibraryConversionStatusEndpoint:
    """Verify GET /library/conversion-status endpoint."""

    def test_library_conversion_status_unregistered_returns_400(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/library/conversion-status")
            assert res.status_code == 400

    def test_library_conversion_status_success(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        pdf_path1 = tmp_path / "paper1.pdf"
        pdf_path1.write_text("pdf1", encoding="utf-8")
        pdf_path2 = tmp_path / "paper2.pdf"
        pdf_path2.write_text("pdf2", encoding="utf-8")

        mock_item1 = MagicMock()
        mock_item1.item_id = 1
        mock_item1.item_key = "ITEM1"
        mock_item1.title = "Deep Learning Paper"

        mock_att1 = MagicMock()
        mock_att1.item_key = "ATT1"
        mock_att1.path = pdf_path1
        mock_att1.content_type = "application/pdf"
        mock_att1.parent_item_key = "ITEM1"

        mock_item2 = MagicMock()
        mock_item2.item_id = 2
        mock_item2.item_key = "ITEM2"
        mock_item2.title = "Unconverted Paper"

        mock_att2 = MagicMock()
        mock_att2.item_key = "ATT2"
        mock_att2.path = pdf_path2
        mock_att2.content_type = "application/pdf"
        mock_att2.parent_item_key = "ITEM2"

        mock_syncer.db.get_all_convertible_attachments.return_value = [
            {
                "attachment_id": 1,
                "attachment_key": "ATT1",
                "parent_item_id": 1,
                "parent_key": "ITEM1",
                "parent_title": "Deep Learning Paper",
                "content_type": "application/pdf",
                "path": pdf_path1,
                "filename": "paper1.pdf",
            },
            {
                "attachment_id": 2,
                "attachment_key": "ATT2",
                "parent_item_id": 2,
                "parent_key": "ITEM2",
                "parent_title": "Unconverted Paper",
                "content_type": "application/pdf",
                "path": pdf_path2,
                "filename": "paper2.pdf",
            },
        ]
        mock_syncer.processor.get_all_metadata.return_value = {
            "ATT1": {"converter_id": "datalab", "char_count": 5000},
        }

        mock_syncer.store.get_all_ingested_item_keys.return_value = {"ATT1"}

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/library/conversion-status")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["total_documents"] == 2
            assert data["converted_count"] == 1
            assert data["indexed_count"] == 1
            assert data["pending_index_count"] == 0
            assert data["converter_counts"]["Datalab (Cloud)"] == 1
            assert data["converter_counts"]["Docling (Local)"] == 0
            assert data["converter_counts"]["MarkItDown (Local)"] == 0
            assert data["unconverted_count"] == 1
            assert len(data["unconverted_items"]) == 1
            assert data["unconverted_items"][0]["key"] == "ATT2"
            assert data["unconverted_items"][0]["title"] == "Unconverted Paper"
            assert data["unconverted_items"][0]["parent_key"] == "ITEM2"
            assert data["pending_index_items"] == []

    def test_library_conversion_status_pending_index(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        pdf_path = tmp_path / "paper1.pdf"
        pdf_path.write_text("pdf1", encoding="utf-8")

        mock_syncer.db.get_all_convertible_attachments.return_value = [
            {
                "attachment_id": 1,
                "attachment_key": "ATT1",
                "parent_item_id": 1,
                "parent_key": "ITEM1",
                "parent_title": "Deep Learning Paper",
                "content_type": "application/pdf",
                "path": pdf_path,
                "filename": "paper1.pdf",
            }
        ]
        mock_syncer.processor.get_all_metadata.return_value = {
            "ATT1": {"converter_id": "docling_local", "char_count": 4000},
        }
        # Ingested keys is empty -> ATT1 is pending index
        mock_syncer.store.get_all_ingested_item_keys.return_value = set()

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/library/conversion-status")
            assert res.status_code == 200
            data = res.json()
            assert data["total_documents"] == 1
            assert data["converted_count"] == 1
            assert data["indexed_count"] == 0
            assert data["pending_index_count"] == 1
            assert len(data["pending_index_items"]) == 1
            assert data["pending_index_items"][0]["key"] == "ATT1"
            assert data["pending_index_items"][0]["converter_id"] == "docling_local"


class TestBatchIndexEndpoint:
    """Verify POST /batch-index endpoint."""

    def test_batch_index_unregistered_returns_400(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/batch-index", json={"keys": ["ATT1"]})
            assert res.status_code == 400

    def test_batch_index_success(self, client, tmp_path) -> None:
        mock_syncer = MagicMock()
        cache_dir = tmp_path / "corpus"
        att_dir = cache_dir / "ATT1"
        att_dir.mkdir(parents=True)
        md_file = att_dir / "document.md"
        md_file.write_text("# Test Document\n\nSome chunk content.", encoding="utf-8")

        mock_syncer.processor.cache_dir = cache_dir
        mock_syncer.db.get_attachment_by_key.return_value = MagicMock(
            item_key="ATT1",
            parent_item_key="ITEM1",
            parent_item_id=1,
            content_type="application/pdf",
            path=tmp_path / "paper.pdf",
        )
        mock_syncer.db.get_item.return_value = MagicMock(
            item_id=1,
            item_key="ITEM1",
            title="Test Paper Title",
            creators=["Author A"],
            year="2024",
        )

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/batch-index", json={"keys": ["ATT1"]})
            assert res.status_code == 200
            assert "text/event-stream" in res.headers["content-type"]
            body = res.text
            assert "batch_start" in body
            assert "item_success" in body
            assert "batch_complete" in body
            mock_syncer.store.ingest_document.assert_called_once()
            mock_syncer.processor.update_indexed_at.assert_called_once_with("ATT1")


class TestStreamRunnerEvents:
    """Tests for _stream_runner_events invocation tracking and error recovery."""

    @pytest.mark.asyncio
    async def test_stream_runner_events_invocation_id(self) -> None:
        from zocrates.fast_api_app import _stream_runner_events

        event = MagicMock()
        event.id = "evt-1"
        event.author = "Dialogue Agent"
        event.invocation_id = "inv-123"
        event.is_final_response.return_value = False
        event.partial = False
        event.content = None
        event.usage_metadata = None
        event.finish_reason = None
        event.model_version = None
        event.long_running_tool_ids = None

        async def fake_gen():
            yield event

        items = [item async for item in _stream_runner_events(fake_gen())]
        assert len(items) == 1
        data = json.loads(items[0]["data"])
        assert data["invocation_id"] == "inv-123"

    @pytest.mark.asyncio
    async def test_stream_runner_events_error_with_invocation_id(self) -> None:
        from zocrates.fast_api_app import _stream_runner_events

        event = MagicMock()
        event.id = "evt-1"
        event.author = "Dialogue Agent"
        event.invocation_id = "inv-456"
        event.is_final_response.return_value = False
        event.partial = False
        event.content = None
        event.usage_metadata = None
        event.finish_reason = None
        event.model_version = None
        event.long_running_tool_ids = None

        async def fake_gen():
            yield event
            raise RuntimeError("Generation failed mid-stream")

        items = [item async for item in _stream_runner_events(fake_gen())]
        assert len(items) == 2
        err_data = json.loads(items[1]["data"])
        assert "error" in err_data
        assert err_data["invocation_id"] == "inv-456"

    @pytest.mark.asyncio
    async def test_stream_runner_events_error_fallback_invocation_id(self) -> None:
        from zocrates.fast_api_app import _stream_runner_events, app

        async def failing_gen():
            raise RuntimeError("Immediate generation failure")
            yield

        mock_user_evt = MagicMock()
        mock_user_evt.author = "user"
        mock_user_evt.invocation_id = "inv-fallback-789"

        mock_sess = MagicMock()
        mock_sess.events = [mock_user_evt]

        mock_session_service = MagicMock()
        mock_session_service.get_session = AsyncMock(return_value=mock_sess)

        mock_runner = MagicMock()
        mock_runner.session_service = mock_session_service

        app.state.runner = mock_runner
        try:
            items = [item async for item in _stream_runner_events(failing_gen(), session_id="test-session")]
            assert len(items) == 1
            err_data = json.loads(items[0]["data"])
            assert "error" in err_data
            assert err_data["invocation_id"] == "inv-fallback-789"
        finally:
            del app.state.runner

    @pytest.mark.asyncio
    async def test_stream_runner_events_error_fallback_skips_stale_turn(self) -> None:
        from zocrates.fast_api_app import _stream_runner_events, app

        async def failing_gen():
            raise RuntimeError("Generation failure before user event creation")
            yield

        # Stale user event from 30 seconds ago
        req_start = time.time()
        mock_user_evt = MagicMock()
        mock_user_evt.author = "user"
        mock_user_evt.invocation_id = "inv-stale-111"
        mock_user_evt.timestamp = req_start - 30.0
        mock_part = MagicMock()
        mock_part.thought = False
        mock_part.text = "Old turn prompt"
        mock_user_evt.content.parts = [mock_part]

        mock_sess = MagicMock()
        mock_sess.events = [mock_user_evt]

        mock_session_service = MagicMock()
        mock_session_service.get_session = AsyncMock(return_value=mock_sess)

        mock_runner = MagicMock()
        mock_runner.session_service = mock_session_service

        app.state.runner = mock_runner
        try:
            items = [
                item
                async for item in _stream_runner_events(
                    failing_gen(),
                    session_id="test-session",
                    request_start_time=req_start,
                    prompt_text="Brand new question",
                )
            ]
            assert len(items) == 1
            err_data = json.loads(items[0]["data"])
            assert "error" in err_data
            # Stale event must NOT be bound as fallback invocation_id
            assert "invocation_id" not in err_data
        finally:
            del app.state.runner

    @pytest.mark.asyncio
    async def test_stream_runner_events_error_fallback_matches_recent_or_prompt(self) -> None:
        from zocrates.fast_api_app import _stream_runner_events, app

        async def failing_gen():
            raise RuntimeError("Generation failure after user event creation")
            yield

        req_start = time.time()

        # Recent user event within 5 seconds
        mock_user_evt = MagicMock()
        mock_user_evt.author = "user"
        mock_user_evt.invocation_id = "inv-recent-222"
        mock_user_evt.timestamp = req_start - 1.0
        mock_part = MagicMock()
        mock_part.thought = False
        mock_part.text = "Tell me about quantum computing"
        mock_user_evt.content.parts = [mock_part]

        mock_sess = MagicMock()
        mock_sess.events = [mock_user_evt]

        mock_session_service = MagicMock()
        mock_session_service.get_session = AsyncMock(return_value=mock_sess)

        mock_runner = MagicMock()
        mock_runner.session_service = mock_session_service

        app.state.runner = mock_runner
        try:
            items = [
                item
                async for item in _stream_runner_events(
                    failing_gen(),
                    session_id="test-session",
                    request_start_time=req_start,
                    prompt_text="Tell me about quantum computing",
                )
            ]
            assert len(items) == 1
            err_data = json.loads(items[0]["data"])
            assert "error" in err_data
            assert err_data.get("invocation_id") == "inv-recent-222"
        finally:
            del app.state.runner

    @pytest.mark.asyncio
    async def test_stream_runner_events_citations_current_turn(self) -> None:
        """Verify citations and references resolve when last audit entry belongs to current turn."""
        from zocrates.fast_api_app import _stream_runner_events, app
        from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
        from google.genai import types

        mock_event = MagicMock()
        mock_event.id = "evt-1"
        mock_event.author = "Dialogue Agent"
        mock_event.invocation_id = "inv-1"
        mock_event.is_final_response.return_value = True
        mock_event.partial = False
        text_part = types.Part.from_text(text="Studies demonstrate that somatic mutations occur [1].")
        mock_event.content = types.Content(role="model", parts=[text_part])
        mock_event.custom_metadata = {}
        mock_event.usage_metadata = None
        mock_event.finish_reason = None
        mock_event.model_version = None
        mock_event.long_running_tool_ids = None

        mock_sess = MagicMock()
        mock_sess.state = {
            RETRIEVED_EVIDENCE: [
                {
                    "invocation_id": "inv-1",
                    "evidence_chunks": [
                        {
                            "chunk_id": "JONSSON::1",
                            "item_key": "99R6SJ7T",
                            "authors": ["Jonsson, H."],
                            "year": "2021",
                            "title": "Twins Study",
                            "heading": "Methods",
                            "text": "Somatic mutation rates.",
                        }
                    ],
                }
            ]
        }

        mock_runner = MagicMock()
        mock_runner.session_service.get_session = AsyncMock(return_value=mock_sess)
        app.state.runner = mock_runner

        async def gen():
            yield mock_event

        try:
            with patch("zocrates.config.get_citations_enabled", return_value=True):
                items = [item async for item in _stream_runner_events(gen(), session_id="test-session")]
                assert len(items) == 1
                data = json.loads(items[0]["data"])
                assert "citations" in data
                assert len(data["citations"]) == 1
                assert data["citations"][0]["index"] == 1
                assert "references" in data
        finally:
            del app.state.runner

    @pytest.mark.asyncio
    async def test_stream_runner_events_citations_skipped_on_stale_turn(self) -> None:
        """Verify conversational turns without a search pipeline run do NOT append stale sources."""
        from zocrates.fast_api_app import _stream_runner_events, app
        from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
        from google.genai import types

        # Turn 2 conversational event: inv-2
        mock_event = MagicMock()
        mock_event.id = "evt-2"
        mock_event.author = "Dialogue Agent"
        mock_event.invocation_id = "inv-2"
        mock_event.is_final_response.return_value = True
        mock_event.partial = False
        text_part = types.Part.from_text(text="You are welcome! Let me know if you need anything else.")
        mock_event.content = types.Content(role="model", parts=[text_part])
        mock_event.custom_metadata = {}
        mock_event.usage_metadata = None
        mock_event.finish_reason = None
        mock_event.model_version = None
        mock_event.long_running_tool_ids = None

        # State has audit entry from Turn 1 (inv-1)
        mock_sess = MagicMock()
        mock_sess.state = {
            RETRIEVED_EVIDENCE: [
                {
                    "invocation_id": "inv-1",
                    "evidence_chunks": [
                        {
                            "chunk_id": "JONSSON::1",
                            "item_key": "99R6SJ7T",
                            "authors": ["Jonsson, H."],
                            "year": "2021",
                            "title": "Twins Study",
                            "heading": "Methods",
                            "text": "Somatic mutation rates.",
                        }
                    ],
                }
            ]
        }

        mock_runner = MagicMock()
        mock_runner.session_service.get_session = AsyncMock(return_value=mock_sess)
        app.state.runner = mock_runner

        async def gen():
            yield mock_event

        try:
            with patch("zocrates.config.get_citations_enabled", return_value=True):
                items = [item async for item in _stream_runner_events(gen(), session_id="test-session")]
                assert len(items) == 1
                data = json.loads(items[0]["data"])
                # Citations MUST NOT be resolved or appended on conversational turn with stale audit
                assert "citations" not in data
                assert "references" not in data
                assert "You are welcome!" in data["parts"][0]["text"]
                assert "# Cited Sources" not in data["parts"][0]["text"]
        finally:
            del app.state.runner

    def test_format_session_for_ui_rehydrates_citations_from_session_state(self) -> None:
        """Verify format_session_for_ui restores citations and references from session state."""
        from zocrates.utils.session_formatting import format_session_for_ui
        from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
        from google.adk.events.event import Event
        from google.genai import types

        text_part = types.Part.from_text(text="Studies demonstrate somatic mutations occur [1].\n\n# Cited Sources\n1. Jonsson (2021)")
        event = Event(
            id="evt-reload-1",
            invocation_id="inv-reload-1",
            author="coordinator_agent",
            content=types.Content(role="model", parts=[text_part]),
            custom_metadata={},
        )

        mock_session = MagicMock()
        mock_session.id = "sess-reload"
        mock_session.events = [event]
        mock_session.state = {
            RETRIEVED_EVIDENCE: [
                {
                    "invocation_id": "inv-reload-1",
                    "citations": [
                        {
                            "index": 1,
                            "token": "[1]",
                            "item_key": "99R6SJ7T",
                            "authors": "Jonsson",
                            "year": "2021",
                            "title": "Twins Study",
                        }
                    ],
                    "references": [
                        {
                            "item_key": "99R6SJ7T",
                            "authors": "Jonsson",
                            "year": "2021",
                            "title": "Twins Study",
                            "tier": 1,
                        }
                    ],
                }
            ]
        }

        res = format_session_for_ui(mock_session)
        assert len(res["messages"]) == 1
        msg = res["messages"][0]
        assert msg["citations"] is not None
        assert len(msg["citations"]) == 1
        assert msg["citations"][0]["index"] == 1
        assert msg["references"] is not None
        assert len(msg["references"]) == 1


class TestItemsDeleteEndpoint:
    """Verify POST /items/delete endpoint."""

    def test_delete_items_unregistered(self, client) -> None:
        """When syncer is not registered, returns 400."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/items/delete", json={"item_keys": ["ITEM1"]})
            assert res.status_code == 400
            assert "not registered" in res.json()["detail"].lower()

    def test_delete_items_success(self, client) -> None:
        """Valid keys are purged via syncer.purge_item and returned in purged list."""
        mock_syncer = MagicMock()
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post("/items/delete", json={"item_keys": ["KEY1", "KEY2", "../BADKEY!"]})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            # Valid keys only (BADKEY! filtered out by regex validation)
            assert data["purged"] == ["KEY1", "KEY2"]
            assert mock_syncer.purge_item.call_count == 2
            mock_syncer.purge_item.assert_any_call("KEY1", regenerate_index=False)
            mock_syncer.purge_item.assert_any_call("KEY2", regenerate_index=False)
            mock_syncer.processor.regenerate_root_index.assert_called_once()


class TestGraphRecomputeEndpoint:
    """Verify POST /graph/recompute endpoint."""

    def test_recompute_unregistered(self, client) -> None:
        """When syncer is not registered, returns 400."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/graph/recompute", json={"item_keys": ["KEY1"]})
            assert res.status_code == 400
            assert "not registered" in res.json()["detail"].lower()

    def test_recompute_with_keys(self, client) -> None:
        """Recomputes specified keys via evaluate_and_persist_edges."""
        mock_syncer = MagicMock()
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.evaluate_and_persist_edges") as mock_eval:
            mock_eval.return_value = []
            res = client.post("/graph/recompute", json={"item_keys": ["KEY1", "KEY2"]})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["recomputed_count"] == 2
            assert mock_eval.call_count == 2
            mock_eval.assert_any_call("KEY1", mock_syncer, True)
            mock_eval.assert_any_call("KEY2", mock_syncer, True)

    def test_recompute_all_keys(self, client) -> None:
        """When no item_keys provided, queries all meta keys and recomputes."""
        mock_syncer = MagicMock()
        mock_syncer.processor.get_all_meta_keys.return_value = {"KEY1", "KEY2", "KEY3"}
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.evaluate_and_persist_edges") as mock_eval:
            mock_eval.return_value = []
            res = client.post("/graph/recompute", json={})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["recomputed_count"] == 3
            assert mock_eval.call_count == 3


class TestBackendLifecycleEndpoints:
    """Verify backend_port in status/config, auto-registration in lifespan, watchdog, and POST /shutdown."""

    def test_status_endpoint_includes_backend_port(self, client) -> None:
        """Verify GET /status includes backend_port in both unregistered and registered states."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert "backend_port" in data

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/mock_zotero")
        mock_syncer.processor.list_cached_keys.return_value = []
        mock_syncer.store._embedder = MagicMock(dimension=768, _model_name="jinaai/jina-embeddings-v5-text-nano")
        mock_syncer.store.active_collection_name = "test_collection"
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/status")
            assert res.status_code == 200
            data = res.json()
            assert "backend_port" in data

    def test_llm_config_backend_port_roundtrip(self, client) -> None:
        """Verify GET /config includes backend_port and POST /config updates it."""
        res = client.get("/config")
        assert res.status_code == 200
        data = res.json()
        assert "backend_port" in data

        with patch("zocrates.config.save_zocrates_config") as mock_save:
            post_res = client.post("/config", json={"backend_port": 9055})
            assert post_res.status_code == 200
            assert mock_save.called
            saved_args = mock_save.call_args[0][0]
            assert saved_args.get("backend_port") == 9055

    @pytest.mark.asyncio
    async def test_lifespan_auto_registration_with_valid_zotero_dir(self, tmp_path: Path) -> None:
        """Verify lifespan auto-initializes services when ZOTERO_DATA_DIR contains zotero.sqlite and storage/."""
        from zocrates.fast_api_app import lifespan

        sqlite_file = tmp_path / "zotero.sqlite"
        sqlite_file.touch()
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        mock_syncer = MagicMock()
        mock_syncer.sync_metadata_staleness = MagicMock(return_value={"status": "clean"})

        with patch.dict(os.environ, {"ZOTERO_DATA_DIR": str(tmp_path)}), \
             patch("zocrates.fast_api_app.initialize_services", return_value=mock_syncer) as mock_init, \
             patch("zocrates.fast_api_app.setup_file_logging") as mock_log, \
             patch("zocrates.utils.session_maintenance.run_session_database_maintenance", new_callable=AsyncMock):
            async with lifespan(app):
                assert mock_init.called
                assert mock_init.call_args[0][0] == tmp_path.resolve()
                assert mock_log.called

    @pytest.mark.asyncio
    async def test_lifespan_skips_auto_registration_when_missing_files(self, tmp_path: Path) -> None:
        """Verify lifespan skips auto-registration when zotero.sqlite or storage/ are absent."""
        from zocrates.fast_api_app import lifespan

        with patch.dict(os.environ, {"ZOTERO_DATA_DIR": str(tmp_path)}), \
             patch("zocrates.fast_api_app.initialize_services") as mock_init:
            async with lifespan(app):
                assert not mock_init.called

    @pytest.mark.asyncio
    async def test_lifespan_starts_watchdog_thread_when_zotero_pid_set(self) -> None:
        """Verify lifespan spawns a live watchdog daemon thread when ZOTERO_PID > 0."""
        from zocrates.fast_api_app import lifespan

        # Use our own live PID so the real daemon watchdog polls os.kill(pid, 0)
        # every 2s forever and can never fire os._exit — no Thread patching, since
        # patching zocrates.fast_api_app.threading.Thread would replace the stdlib
        # threading.Thread process-wide and hang ThreadPoolExecutor at shutdown.
        with patch.dict(os.environ, {"ZOTERO_PID": str(os.getpid())}, clear=False):
            async with lifespan(app):
                thread = app.state.parent_watchdog_thread
                assert isinstance(thread, threading.Thread)
                assert thread.daemon is True
                assert thread.name == "ZoteroParentWatchdog"
                assert thread.is_alive()

    def test_parent_watchdog_terminates_on_dead_process(self) -> None:
        """Verify _parent_watchdog calls exit_fn(0) when parent PID does not exist."""
        from zocrates.fast_api_app import _parent_watchdog

        mock_exit = MagicMock()
        with patch("os.kill", side_effect=ProcessLookupError):
            _parent_watchdog(parent_pid=999999, poll_interval=0.001, exit_fn=mock_exit)
            mock_exit.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_shutdown_endpoint_schedules_termination(self) -> None:
        """Verify POST /shutdown logic schedules _trigger_shutdown at 0.5s via loop.call_later."""
        from zocrates.fast_api_app import _trigger_shutdown, shutdown_endpoint

        # Do NOT patch "asyncio.get_running_loop": that is a process-wide module-attr
        # patch which anyio's TaskGroup.start_soon also resolves inside the TestClient
        # portal loop, leaving the request task unscheduled and the test hung. Call the
        # endpoint directly on the real pytest-asyncio loop and patch loop.call_later
        # on the loop instance only.
        loop = asyncio.get_running_loop()
        with patch.object(loop, "call_later") as mock_call_later, \
             patch("zocrates.fast_api_app._trigger_shutdown") as mock_trigger:
            res = await shutdown_endpoint()

        assert res == {"status": "ok", "message": "Backend shutting down"}
        mock_call_later.assert_called_once_with(0.5, mock_trigger)


class TestHealthHeartbeatEndpoint:
    """Verify fast zero-I/O GET /health endpoint, log suppression, and hot-swap."""

    def test_health_unauthenticated_registered_and_unregistered(self) -> None:
        unauth_client = TestClient(app)

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/zotero_health_dir")
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = unauth_client.get("/health")
            assert res.status_code == 200
            assert res.json() == {
                "status": "ready",
                "zotero_data_dir": "/tmp/zotero_health_dir",
            }

        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")), \
             patch("zocrates.fast_api_app._status_zotero_data_dir_hint", return_value="/tmp/hint_dir"):
            res_unreg = unauth_client.get("/health")
            assert res_unreg.status_code == 200
            assert res_unreg.json() == {
                "status": "not_registered",
                "zotero_data_dir": "/tmp/hint_dir",
            }

    def test_health_authenticated_zero_io_payload(self, client, tmp_path: Path) -> None:
        from zocrates.config import save_zocrates_config

        save_zocrates_config(
            {
                "llm_model": "openrouter/anthropic/claude-sonnet-4",
                "llm_provider": "openrouter_llm",
                "auto_render_mermaid_diagrams": False,
            },
            zotero_data_dir=tmp_path,
        )

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = tmp_path

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.check_config_freshness", return_value=False), \
             patch("zocrates.rag.hardware.get_system_hardware_info") as mock_hw, \
             patch("zocrates.utils.session_maintenance.get_session_storage_stats") as mock_sess_stats, \
             patch("zocrates.config.get_secure_api_key") as mock_get_secure_api_key:
            res = client.get("/health")

        assert res.status_code == 200
        data = res.json()
        assert data == {
            "status": "ready",
            "zotero_data_dir": str(tmp_path),
            "model": "openrouter/anthropic/claude-sonnet-4",
            "llm_provider": "openrouter_llm",
        }
        mock_syncer.processor.list_cached_keys.assert_not_called()
        mock_hw.assert_not_called()
        mock_sess_stats.assert_not_called()
        mock_get_secure_api_key.assert_not_called()

    def test_health_authenticated_hot_swaps_model_when_config_modified(self, client, tmp_path: Path) -> None:
        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = tmp_path

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.check_config_freshness", return_value=True), \
             patch("zocrates.llm.get_llm_model", return_value="hot-swapped-llm") as mock_get_model, \
             patch("zocrates.agents.app.set_agent_models") as mock_set_models:
            res = client.get("/health")

        assert res.status_code == 200
        mock_get_model.assert_called_once_with(tmp_path)
        mock_set_models.assert_called_once_with("hot-swapped-llm", zotero_data_dir=tmp_path)

    def test_health_suppresses_info_request_logging_while_status_logs(self, client, caplog) -> None:
        import logging

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = Path("/tmp/zotero_log_test")
        mock_syncer.processor.list_cached_keys.return_value = []
        mock_syncer.store._embedder = MagicMock(dimension=768, _model_name="jinaai/jina-embeddings-v5-text-nano")
        mock_syncer.store.active_collection_name = "col1"

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.check_config_freshness", return_value=False):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="zocrates.fast_api_app"):
                res_health = client.get("/health")
                assert res_health.status_code == 200
                assert "/health" not in caplog.text

                res_status = client.get("/status")
                assert res_status.status_code == 200
                assert "--> GET /status" in caplog.text
                assert "<-- GET /status" in caplog.text

    def test_get_llm_config_hot_swaps_model_when_config_modified(self, client, tmp_path: Path) -> None:
        old_state_dir = getattr(app.state, "zotero_data_dir", None)
        try:
            app.state.zotero_data_dir = None
            with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
                 patch("zocrates.fast_api_app.check_config_freshness", return_value=True) as mock_fresh, \
                 patch("zocrates.llm.get_llm_model", return_value="prefs-hot-swap-llm") as mock_get_model, \
                 patch("zocrates.agents.app.set_agent_models") as mock_set_models:
                res = client.get("/config")
            assert res.status_code == 200
            mock_fresh.assert_called_once_with(tmp_path)
            mock_get_model.assert_called_once_with(tmp_path)
            mock_set_models.assert_called_once_with("prefs-hot-swap-llm", zotero_data_dir=tmp_path)
        finally:
            app.state.zotero_data_dir = old_state_dir


class TestDiagnosticReportEndpoint:
    """Verify POST /diagnostics/report authentication, validation, and sanitization."""

    def test_unauthenticated_request_rejected_with_401(self) -> None:
        unauth_client = TestClient(app)
        res = unauth_client.post(
            "/diagnostics/report",
            json={"error_message": "401 Unauthorized"},
        )
        assert res.status_code == 401

    def test_log_tail_lines_validation_bounds_50_to_500(self, client) -> None:
        res_low = client.post("/diagnostics/report", json={"log_tail_lines": 10})
        assert res_low.status_code == 422

        res_high = client.post("/diagnostics/report", json={"log_tail_lines": 1000})
        assert res_high.status_code == 422

    def test_authorized_default_toggles_scrubs_secrets_and_omits_private_content(
        self, client, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "zocrates.log").write_text("PRIVATE_BACKEND_LOG_LINE\n", encoding="utf-8")

        mock_syncer = MagicMock()
        mock_syncer._zotero_data_dir = tmp_path

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.post(
                "/diagnostics/report",
                json={
                    "error_message": "Failed with key sk-or-v1-aabbccddeeff00112233 at /Users/alice/Zotero",
                    "frontend_metadata": {
                        "zotero_version": "10.0.2",
                        "zotero_platform": "MacIntel",
                        "extension_version": "0.1.0",
                        "window_mode": "sidebar",
                        "active_items": [
                            {"key": "ITEMKEY1", "title": "Secret Paper Title"}
                        ],
                        "notifications": [
                            {
                                "type": "error",
                                "text": "Auth failed for user@stanford.edu",
                                "timestamp": "12:00:00",
                            }
                        ],
                    },
                },
            )

        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        md = body["markdown"]
        assert "sk-or-v1-" not in md
        assert "/Users/alice" not in md
        assert "user@stanford.edu" not in md
        assert "Secret Paper Title" not in md
        assert "PRIVATE_BACKEND_LOG_LINE" not in md
        assert "[Key: ITEMKEY1] <Title omitted>" in md
        assert "[REDACTED_OPENROUTER_KEY]" in md

    def test_authorized_custom_toggles_and_unregistered_fallback(
        self, client, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "zocrates.log").write_text(
            "2026-04-01 ERROR Crash in C:\\Users\\Bob\\Zotero\\file.pdf\n",
            encoding="utf-8",
        )

        with (
            patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")),
            patch("zocrates.fast_api_app._status_zotero_data_dir_hint", return_value=str(tmp_path)),
        ):
            res = client.post(
                "/diagnostics/report",
                json={
                    "session_id": "sess_nonexistent",
                    "error_message": "Conversion crash",
                    "include_prompts": True,
                    "include_item_titles": True,
                    "include_logs": True,
                    "log_tail_lines": 50,
                    "frontend_metadata": {
                        "active_items": [
                            {"key": "ITEMKEY2", "title": "Included Paper Title"}
                        ],
                        "last_user_prompt": "Explain equation 3",
                    },
                },
            )

        assert res.status_code == 200
        md = res.json()["markdown"]
        assert "[Key: ITEMKEY2] Included Paper Title" in md
        assert "Explain equation 3" in md
        assert "Bob" not in md
        assert r"<HOME>\Zotero\file.pdf" in md
