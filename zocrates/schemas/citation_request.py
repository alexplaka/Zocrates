from __future__ import annotations

from pydantic import BaseModel


class SentenceAttributionRequest(BaseModel):
    """Request body for ``POST /citations/sentence_attribution``."""

    claim: str
    source_text: str
    session_id: str | None = None
    invocation_id: str | None = None
    chunk_index: int | None = 0
    occurrence: int | None = 0
