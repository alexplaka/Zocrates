from typing import Any

from pydantic import BaseModel, Field


class DiagnosticReportRequest(BaseModel):
    """Request body for ``POST /diagnostics/report``."""

    session_id: str | None = None
    invocation_id: str | None = None
    error_message: str | None = None
    include_prompts: bool = False
    include_item_titles: bool = False
    include_logs: bool = False
    log_tail_lines: int = Field(default=250, ge=50, le=500)
    frontend_metadata: dict[str, Any] | None = None
