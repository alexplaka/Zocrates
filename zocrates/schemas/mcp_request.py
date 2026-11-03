from pydantic import BaseModel


class MCPSearchRequest(BaseModel):
    """Request body for POST /mcp/search."""

    query: str
    limit: int = 10
    item_key: str | list[str] | None = None
    chunk_type: str | list[str] | None = None
    exclude_chunk_ids: list[str] | None = None


class MCPReadRequest(BaseModel):
    """Request body for POST /mcp/documents/{item_key}/read."""

    asset_id: str | list[str] | None = None
    page: int | None = None
