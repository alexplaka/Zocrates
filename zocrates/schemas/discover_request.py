from typing import Any

from pydantic import BaseModel


class DiscoverSearchRequest(BaseModel):
    """Request body for ``POST /discover/search``."""

    query: str
    limit: int = 10
    year_from: int | None = None
    year_to: int | None = None
    is_oa_only: bool = False


class DiscoverResolveRequest(BaseModel):
    """Request body for ``POST /discover/resolve``."""

    identifier: str


class DiscoverPrepareBatchRequest(BaseModel):
    """Request body for ``POST /discover/prepare_batch``."""

    candidates: list[dict[str, Any]]


class DiscoverCleanupBatchRequest(BaseModel):
    """Request body for ``POST /discover/cleanup_batch``."""

    batch_token: str
