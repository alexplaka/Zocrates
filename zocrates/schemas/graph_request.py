from pydantic import BaseModel


class GraphRecomputeRequest(BaseModel):
    """Request body for ``POST /graph/recompute``."""

    item_keys: list[str] | None = None
