from typing import Any

from pydantic import BaseModel, Field

from zocrates.schemas.item_request import ActiveItem, SnippetItem


class ChatRequest(BaseModel):
    """Request body for ``POST /chat``."""

    prompt: str
    session_id: str
    active_items: list[ActiveItem] | None = None
    snippets: list[SnippetItem] | None = Field(default=None, max_length=20)


class ResumeChatRequest(BaseModel):
    """Payload for resuming an interrupted human-in-the-loop chat invocation."""

    session_id: str
    invocation_id: str
    interrupt_id: str
    response: dict[str, Any]
