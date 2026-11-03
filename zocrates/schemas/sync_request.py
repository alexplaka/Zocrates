from pydantic import BaseModel


class SyncRequest(BaseModel):
    """Request body for ``POST /sync``.

    Attributes:
        item_ids: Specific item IDs to ingest. If ``None``, a full library
            scan is performed.
    """

    item_ids: list[int] | None = None


