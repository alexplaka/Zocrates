"""Backend client for Zocrates MCP Server.

Dispatches tool calls and resource reads over HTTP to the running Zocrates
FastAPI backend service (:8765).

STRICT INVARIANT:
Stdout is reserved strictly for JSON-RPC 2.0 protocol frames.
All logging, diagnostic messages, and tracebacks MUST route exclusively to sys.stderr.
"""

import logging
import os
from pathlib import Path
import sys
from typing import Any, Self

import httpx


class _StderrStreamHandler(logging.StreamHandler):
    """StreamHandler that dynamically resolves sys.stderr on emit."""

    @property
    def stream(self) -> Any:  # type: ignore[override]
        """Dynamically resolve and return current sys.stderr stream.

        Returns:
            The current sys.stderr stream object.
        """
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        """No-op setter to maintain StreamHandler compatibility.

        Args:
            value: Ignored stream value.
        """
        pass


logger = logging.getLogger("zocrates.mcp.client")
if not logger.handlers:
    _handler = _StderrStreamHandler()
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [mcp.client] %(message)s")
    )
    logger.addHandler(_handler)
logger.propagate = False


class BackendClient:
    """HTTP client for communicating with the local Zocrates FastAPI backend."""

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        auth_token: str | None = None,
    ) -> None:
        """Initialize the MCP backend client.

        Args:
            base_url: Optional backend base URL (defaults to ZOCRATES_BACKEND_URL or http://127.0.0.1:8765).
            client: Optional preconfigured httpx.Client instance.
            auth_token: Optional authentication token (auto-resolved if omitted).
        """
        self.base_url = (
            base_url or os.environ.get("ZOCRATES_BACKEND_URL", "http://127.0.0.1:8765")
        ).rstrip("/")
        self._auth_token = (
            auth_token.strip()
            if auth_token is not None
            else self._resolve_auth_token()
        )
        timeout = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
        self._client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def _resolve_auth_token() -> str:
        """Resolve Zocrates auth token from ZOCRATES_AUTH_TOKEN env or backend.auth sidecar."""
        env_token = os.environ.get("ZOCRATES_AUTH_TOKEN", "").strip()
        if env_token:
            return env_token
        candidates: list[Path] = []
        if env_dir := os.environ.get("ZOTERO_DATA_DIR"):
            candidates.append(Path(env_dir) / "zocrates-data" / "backend.auth")
        try:
            home = Path.home()
            for pattern in (
                "Zotero*/zocrates-data/backend.auth",
                "Zotero*/*/zocrates-data/backend.auth",
                ".var/app/org.zotero.Zotero/data/zotero/zocrates-data/backend.auth",
                ".zotero/zotero/*/zocrates-data/backend.auth",
            ):
                candidates.extend(home.glob(pattern))
        except Exception:
            pass
        for auth_path in candidates:
            try:
                if auth_path.is_file():
                    token = auth_path.read_text(encoding="utf-8").strip()
                    if token:
                        return token
            except OSError:
                continue
        return ""

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Execute HTTP request against the Zocrates backend with robust error mappings."""
        url = f"{self.base_url}{path}"
        token = self._auth_token or self._resolve_auth_token()
        if token:
            self._auth_token = token
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Authorization", f"Bearer {token}")
            kwargs["headers"] = headers
        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.ConnectError:
            logger.error("Failed to connect to Zocrates backend at %s", self.base_url)
            return {
                "status": "error",
                "message": (
                    "Zocrates backend service is offline. Please open Zotero with "
                    "Zocrates enabled to access your active library."
                ),
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 403:
                return {
                    "status": "error",
                    "message": (
                        "MCP server access is disabled in Zocrates preferences. "
                        "Please enable it in Zotero Preferences -> Zocrates to allow "
                        "external AI tools to query your library."
                    ),
                }
            if status_code == 503:
                return {
                    "status": "error",
                    "message": (
                        "Zotero library is not yet registered or synced. "
                        "Please open Zotero with Zocrates enabled."
                    ),
                }
            try:
                data = exc.response.json()
                detail = data.get("detail") or data.get("message") or exc.response.text
            except Exception:
                detail = exc.response.text or str(exc)
            return {
                "status": "error",
                "message": detail,
            }
        except httpx.TimeoutException:
            logger.error("Request to Zocrates backend timed out: %s %s", method, url)
            return {
                "status": "error",
                "message": (
                    "Zocrates backend timed out while processing retrieval. "
                    "The server may be busy with a batch-indexing or conversion task. "
                    "Please retry shortly."
                ),
            }
        except Exception as exc:
            logger.error("Unexpected error during backend request: %s", exc)
            return {
                "status": "error",
                "message": f"Unexpected backend communication error: {exc}",
            }

    def search(
        self,
        query: str,
        item_key: str | list[str] | None = None,
        chunk_type: str | list[str] | None = None,
        limit: int = 10,
        exclude_chunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Perform hybrid semantic + BM25 keyword search over library chunks."""
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if item_key is not None:
            payload["item_key"] = item_key
        if chunk_type is not None:
            payload["chunk_type"] = chunk_type
        if exclude_chunk_ids is not None:
            payload["exclude_chunk_ids"] = exclude_chunk_ids

        return self._request("POST", "/mcp/search", json=payload)

    def get_toc(self, item_key: str) -> dict[str, Any]:
        """Retrieve Table of Contents and outline metadata for a document."""
        return self._request("GET", f"/mcp/documents/{item_key}/toc")

    def read_document(
        self,
        item_key: str,
        asset_id: str | list[str] | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Read full document Markdown or targeted assets (sections, figures, tables)."""
        payload: dict[str, Any] = {}
        if asset_id is not None:
            payload["asset_id"] = asset_id
        if page is not None:
            payload["page"] = page

        return self._request("POST", f"/mcp/documents/{item_key}/read", json=payload)

    def get_relations(self, item_key: str, ceiling: int = 25) -> dict[str, Any]:
        """Retrieve verified related papers from the literature knowledge graph."""
        return self._request(
            "GET",
            f"/mcp/documents/{item_key}/relations",
            params={"ceiling": ceiling},
        )

    def get_catalog(self) -> dict[str, Any]:
        """Retrieve root-level index.md catalog listing all converted library items."""
        return self._request("GET", "/mcp/catalog")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        """Enter context manager, returning client instance.

        Returns:
            BackendClient instance.
        """
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit context manager, closing underlying HTTP client resources.

        Args:
            *args: Exception type, value, and traceback if raised.
        """
        self.close()
