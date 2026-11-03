from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    """Request body for ``POST /sessions``."""

    session_id: str | None = None
    title: str | None = "New Chat"


class RewindSessionRequest(BaseModel):
    """Request body for ``POST /sessions/{session_id}/rewind``."""

    invocation_id: str
